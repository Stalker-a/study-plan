"""Учебный сайт со значком в трее Windows.

Сервер работает в фоновом потоке, значок — в главном. Пока значок виден, сайт запущен.
«Остановить сервер» в меню (или падение сервера) убирает значок и завершает процесс.

Запуск без окна консоли: .venv\\Scripts\\pythonw.exe tray.py (так делают start.bat и автозагрузка).
"""
import json
import os
import subprocess
import threading
import webbrowser

import pystray
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from waitress.server import create_server

from app import BASE_DIR, INSTANCE_DIR, _log_to_file_if_hidden, _port_is_busy, create_app

ICON_SIZE = 64
BRAND_COLOR = (91, 91, 214)
FONT_PATH = r"C:\Windows\Fonts\segoeuib.ttf"
CREATE_NO_WINDOW = 0x08000000  # чтобы tailscale/clip не мигали окном консоли


def make_icon_image() -> Image.Image:
    """Фирменный значок: скруглённый квадрат цвета бренда с буквой «У»."""
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, ICON_SIZE - 3, ICON_SIZE - 3), radius=14, fill=BRAND_COLOR)
    try:
        font = ImageFont.truetype(FONT_PATH, 40)
    except OSError:
        font = ImageFont.load_default()
    draw.text((ICON_SIZE / 2, ICON_SIZE / 2 + 1), "У", font=font, fill="white", anchor="mm")
    return image


def tailscale_url() -> str | None:
    """Адрес сайта для телефона в сети Tailscale; None, если Tailscale недоступен."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=5,
                             creationflags=CREATE_NO_WINDOW).stdout
        name = json.loads(out)["Self"]["DNSName"].rstrip(".")
        return f"https://{name}" if name else None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["clip"], input=text.encode("utf-16-le"), creationflags=CREATE_NO_WINDOW, check=False)


class TrayServer:
    def __init__(self, host: str, port: int):
        self.local_url = f"http://{host}:{port}"
        self.phone_url = tailscale_url()
        self.server = create_server(create_app(), host=host, port=port, threads=8)
        self.icon = pystray.Icon("study-site", make_icon_image(),
                                 f"Учебный сайт — работает\n{self.local_url}", self._menu())

    def _menu(self) -> pystray.Menu:
        items = [pystray.MenuItem("Открыть сайт", self._open_site, default=True)]
        if self.phone_url:
            items.append(pystray.MenuItem("Скопировать адрес для телефона", self._copy_phone_url))
        items += [
            pystray.MenuItem("Открыть журнал", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Остановить сервер", self._stop),
        ]
        return pystray.Menu(*items)

    def _open_site(self, *_):
        webbrowser.open(self.local_url)

    def _copy_phone_url(self, *_):
        copy_to_clipboard(self.phone_url)
        self.icon.notify(self.phone_url, "Адрес для телефона скопирован")

    def _open_log(self, *_):
        log = INSTANCE_DIR / "server.log"
        if log.exists():
            os.startfile(log)  # откроется в Блокноте

    def _stop(self, *_):
        print("Остановка из меню значка", flush=True)
        self.server.close()
        self.icon.stop()

    def _serve(self):
        try:
            self.server.run()
        except Exception as e:  # сервер упал — значок не должен врать, что всё работает
            print(f"Сервер упал: {e!r}", flush=True)
        finally:
            self.icon.stop()

    def run(self):
        threading.Thread(target=self._serve, name="waitress", daemon=True).start()
        print(f"Сервер: {self.local_url}; телефон: {self.phone_url or 'Tailscale недоступен'}; значок в трее", flush=True)
        self.icon.run()  # блокирует, пока значок не убран
        self.server.task_dispatcher.shutdown(timeout=5)
        print("Сервер остановлен", flush=True)


def main():
    _log_to_file_if_hidden()
    load_dotenv(BASE_DIR / ".env")  # HOST/PORT из .env нужны до создания приложения
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if _port_is_busy(host, port):
        raise SystemExit(f"Порт {port} уже занят — сайт уже запущен (значок должен быть в трее).")
    TrayServer(host, port).run()


if __name__ == "__main__":
    main()
