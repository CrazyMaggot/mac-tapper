#!/usr/bin/env python3
"""Собирает тонкую обёртку «Таппер.app», которая запускает tapper.py через uv.

Приложение весит килобайты и не содержит копии Python: внутри лежит запускающий
скрипт, указывающий на эту папку проекта. Правки в коде подхватываются сразу,
пересобирать обёртку после них не нужно.

    uv run build_app.py                 # собрать в /Applications
    uv run build_app.py --into .        # собрать рядом с проектом
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Таппер"
BUNDLE_ID = "com.crazymaggot.tapper"
VERSION = "0.1.0"
EXECUTABLE = "tapper-launcher"

PROJECT = Path(__file__).resolve().parent
LOG = "$HOME/Library/Logs/tapper.log"

LAUNCHER = """#!/bin/sh
# Обёртка над проектом. Приложения, запущенные из Finder, не видят PATH
# из вашего терминала, поэтому пути прописаны целиком.
#
# Питон запускается через exec, без промежуточного процесса: так система
# считает работающей программой само приложение, и «Универсальный доступ»
# выдаётся «Тапперу», а не безымянному питону.
PROJECT={project}
UV={uv}

mkdir -p "$HOME/Library/Logs"
exec >>"{log}" 2>&1
echo "--- запуск $(date) ---"

fail() {{
    osascript -e "display alert \"Таппер не запустился\" message \"$1\""
    exit 1
}}

[ -f "$PROJECT/tapper.py" ] || fail "Папка проекта не найдена: {project_plain}. Она переехала? Пересоберите приложение."
[ -x "$UV" ] || fail "Не найден uv по пути {uv}. Установите uv или пересоберите приложение."

"$UV" sync --project "$PROJECT" --quiet || echo "uv sync отработал с ошибкой, пробуем запуститься на том, что есть"

PY="$PROJECT/.venv/bin/python3"
[ -x "$PY" ] || fail "Окружение проекта не собрано. Выполните uv sync в папке проекта. Подробности в ~/Library/Logs/tapper.log"

TAPPER_BUNDLE={bundle}
export TAPPER_BUNDLE

exec "$PY" "$PROJECT/tapper.py"
"""


def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/uv", "/usr/local/bin/uv", str(Path.home() / ".local/bin/uv")):
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("uv не найден, обёртке некуда указывать")


def make_icon(resources: Path) -> str | None:
    """Рисует простую иконку. Если не вышло, приложение обойдётся системной."""
    try:
        from AppKit import (
            NSBezierPath, NSBitmapImageRep, NSColor, NSFont, NSImage,
            NSMakeRect, NSMutableParagraphStyle, NSString,
        )
        from Foundation import NSMutableDictionary
    except Exception:
        return None

    iconset = resources / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    try:
        for size in (16, 32, 64, 128, 256, 512, 1024):
            image = NSImage.alloc().initWithSize_((size, size))
            image.lockFocus()
            rect = NSMakeRect(size * 0.06, size * 0.06, size * 0.88, size * 0.88)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.35, 0.62, 1.0).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                rect, size * 0.22, size * 0.22
            ).fill()

            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(2)  # по центру
            attrs = NSMutableDictionary.dictionary()
            attrs["NSFont"] = NSFont.boldSystemFontOfSize_(size * 0.58)
            attrs["NSColor"] = NSColor.whiteColor()
            attrs["NSParagraphStyle"] = style
            text = NSString.stringWithString_("T")
            text_size = text.sizeWithAttributes_(attrs)
            text.drawInRect_withAttributes_(
                NSMakeRect(0, (size - text_size.height) / 2, size, text_size.height), attrs
            )
            image.unlockFocus()

            rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            png = rep.representationUsingType_properties_(4, {})  # 4 = PNG
            name = "icon_512x512@2x.png" if size == 1024 else f"icon_{size}x{size}.png"
            (iconset / name).write_bytes(bytes(png))

        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")],
            check=True, capture_output=True,
        )
    except Exception as error:
        print(f"  иконку нарисовать не вышло ({error}), берём системную")
        shutil.rmtree(iconset, ignore_errors=True)
        return None

    shutil.rmtree(iconset, ignore_errors=True)
    return "AppIcon"


def build(destination: Path) -> Path:
    app = destination / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)

    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    uv = find_uv()
    launcher = macos / EXECUTABLE
    launcher.write_text(
        LAUNCHER.format(
            bundle=shell_quote(str(app)),
            project=shell_quote(str(PROJECT)),
            project_plain=str(PROJECT),
            uv=uv,
            log=LOG,
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    icon = make_icon(resources)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    }
    if icon:
        info["CFBundleIconFile"] = icon
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=False, capture_output=True)
    subprocess.run(["touch", str(app)], check=False)
    return app


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка обёртки «Таппер.app»")
    parser.add_argument("--into", default="/Applications", help="куда положить приложение")
    args = parser.parse_args()

    destination = Path(args.into).expanduser().resolve()
    if not destination.is_dir():
        raise SystemExit(f"Нет такой папки: {destination}")

    app = build(destination)
    print(f"Собрано: {app}")
    print(f"Указывает на проект: {PROJECT}")
    print("Первый запуск попросит «Универсальный доступ» уже для этого приложения.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
