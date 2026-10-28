#!/usr/bin/env python3
"""Build a device IPA and atomically publish it to the owner's shared iCloud folder."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile


DEFAULT_CLOUD = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/IPA Shelf"


def metadata(path):
    with zipfile.ZipFile(path) as archive:
        mains = [i for i in archive.infolist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", i.filename)]
        if len(mains) != 1 or mains[0].file_size > 4 * 1024 * 1024:
            raise ValueError("Ожидался один Payload/*.app/Info.plist")
        info = plistlib.loads(archive.read(mains[0]))
        for key in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion"):
            if not isinstance(info.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", info[key]):
                raise ValueError(f"Некорректный {key}")
        platforms = info.get("CFBundleSupportedPlatforms", [])
        if "iPhoneSimulator" in platforms or (platforms and "iPhoneOS" not in platforms):
            raise ValueError("Для публикации нужна сборка iPhoneOS, не симулятор")
        return info


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def version(info):
    # Apple versions are numeric. Refuse ambiguous ordering rather than guessing.
    result = []
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        text = info[key]
        if not re.fullmatch(r"\d+(\.\d+)*", text):
            raise ValueError("Для сравнения версий нужны числовые version/build")
        parts = list(map(int, text.split(".")))
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        result.append(tuple(parts))
    return tuple(result)


def publish(source, cloud, force=False):
    source, cloud = Path(source).resolve(), Path(cloud).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"IPA не найден: {source}")
    cloud.mkdir(parents=True, exist_ok=True)
    # No .ipa extension until copy, CRC and hash verification have finished.
    fd, temporary = tempfile.mkstemp(prefix=".incoming-", suffix=".uploading", dir=cloud)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        info = metadata(temporary)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("Нарушена целостность ZIP")
        checksum = digest(temporary)
        if digest(source) != checksum:
            raise ValueError("Исходный IPA изменился при копировании")
        bundle = info["CFBundleIdentifier"]
        filename = f"{bundle}--{info['CFBundleShortVersionString']}--{info['CFBundleVersion']}.ipa"
        target = cloud / filename
        lock_root = Path(tempfile.gettempdir()) / f"ipashelf-publish-{os.getuid()}"
        lock_root.mkdir(mode=0o700, exist_ok=True)
        lock_id = hashlib.sha256(f"{cloud}/{bundle}".encode()).hexdigest()
        with (lock_root / lock_id).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old_files = []
            for old in cloud.glob(f"{bundle}--*.ipa"):
                if old.is_symlink():
                    raise ValueError("В папке публикации обнаружена ссылка вместо IPA")
                previous = metadata(old)
                if previous["CFBundleIdentifier"] != bundle:
                    raise ValueError("Bundle ID файла не соответствует его имени")
                if not force and version(previous) > version(info):
                    raise ValueError("В iCloud уже есть более новая версия. Для отката нужен --force")
                if not force and version(previous) == version(info) and digest(old) != checksum:
                    raise ValueError("Version/build уже опубликованы с другим содержимым: поднимите номер или используйте --force")
                old_files.append(old)
            if target.exists() and digest(target) == checksum:
                print(f"Уже опубликовано: {target}")
                return target
            os.replace(temporary, target)
            # Only this bundle's managed releases; other apps and original folders are untouched.
            for old in old_files:
                if old != target:
                    old.unlink()
        print(f"Опубликовано: {target}")
        return target
    finally:
        temporary.unlink(missing_ok=True)


def build(args):
    root = Path(args.project_root).resolve()
    derived = (root / args.derived_data).resolve()
    derived.mkdir(parents=True, exist_ok=True)
    subprocess.run(["xcodegen", "generate"], cwd=root, check=True)
    command = ["xcodebuild", "-project", f"{args.scheme}.xcodeproj", "-scheme", args.scheme,
               "-configuration", "Release", "-destination", "generic/platform=iOS",
               "-derivedDataPath", str(derived), "-clonedSourcePackagesDirPath", str(derived / "SourcePackages"),
               "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=", "build"]
    log = derived / "ipa-release.log"
    print(f"Сборка {args.scheme}; журнал: {log}", flush=True)
    with log.open("w") as output:
        result = subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(f"Сборка не прошла. Журнал: {log}")
    app = derived / "Build/Products/Release-iphoneos" / f"{args.scheme}.app"
    info = plistlib.loads((app / "Info.plist").read_bytes())
    output = root / "build/dist"
    output.mkdir(parents=True, exist_ok=True)
    # Fixed local staging name; the cloud filename is always derived from the actual plist.
    archive = output / f"{args.scheme}-unsigned.ipa"
    with tempfile.TemporaryDirectory(prefix="ipa-release-") as staging:
        stage = Path(staging)
        (stage / "Payload").mkdir()
        shutil.copytree(app, stage / "Payload" / app.name, symlinks=True)
        temporary = stage / "release.ipa"
        subprocess.run(["/usr/bin/zip", "-qry", str(temporary), "Payload"], cwd=stage, check=True)
        # Keep replacement on the same filesystem even if /tmp is on another volume.
        fd, local = tempfile.mkstemp(prefix=".release-", suffix=".uploading", dir=output)
        os.close(fd)
        try:
            shutil.copyfile(temporary, local)
            metadata(local)
            os.replace(local, archive)
        finally:
            Path(local).unlink(missing_ok=True)
    print(f"Готово: {args.scheme} {info.get('CFBundleShortVersionString')} ({info.get('CFBundleVersion')}), {archive}")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root")
    parser.add_argument("--scheme")
    parser.add_argument("--derived-data", default="build/ipa")
    parser.add_argument("--publish-only", type=Path, help="Опубликовать готовый IPA без пересборки")
    parser.add_argument("--cloud", type=Path, default=Path(os.environ.get("IPA_SHELF_DIR", DEFAULT_CLOUD)))
    parser.add_argument("--no-cloud", action="store_true")
    parser.add_argument("--force", action="store_true", help="Разрешить замену той же версии или откат")
    args = parser.parse_args()
    if not args.publish_only and not (args.project_root and args.scheme):
        parser.error("Нужны --project-root и --scheme либо --publish-only")
    try:
        source = args.publish_only if args.publish_only else build(args)
        if not args.no_cloud:
            publish(source, args.cloud, args.force)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Ошибка выпуска: {error}\n")


if __name__ == "__main__":
    main()
