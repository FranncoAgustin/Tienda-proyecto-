# run_server.py
import os, sys, socket, traceback
from pathlib import Path

PORT = os.environ.get("PORT", "8000")
BIND = os.environ.get("BIND", "127.0.0.1")     # poné 127.0.0.1 para probar local
USE_WAITERSS = os.environ.get("USE_WAITRESS", "0") == "1"  # opcional

def resolve_paths():
    """
    APP_DIR: carpeta de código (templates, shop, manage.py, etc.)
    RUNTIME_DIR: carpeta donde está el .exe (para db/media en modo portable)
    """
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller: nosotros copiamos todo dentro de app/
        APP_DIR = Path(sys._MEIPASS) / "app"
        RUNTIME_DIR = Path(sys.executable).parent
    else:
        APP_DIR = Path(__file__).resolve().parent
        RUNTIME_DIR = APP_DIR
    return APP_DIR, RUNTIME_DIR

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def banner(APP_DIR, RUNTIME_DIR):
    print("="*70)
    print("Tienda — iniciando")
    print(f"APP_DIR     = {APP_DIR}")
    print(f"RUNTIME_DIR = {RUNTIME_DIR}")
    print(f"DJANGO_SETTINGS_MODULE = {os.environ.get('DJANGO_SETTINGS_MODULE')}")
    print(f"Modo        = {'waitress' if USE_WAITERSS else 'runserver (call_command)'}")
    print(f"Bind        = {BIND}:{PORT}")
    print("="*70, flush=True)

def main():
    APP_DIR, RUNTIME_DIR = resolve_paths()

    # 1) fijar cwd y sys.path para que Django vea el proyecto
    os.chdir(APP_DIR)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

    # 2) apuntar a settings correctos (ajustá si tu paquete es distinto)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tienda.settings")

    banner(APP_DIR, RUNTIME_DIR)

    try:
        import django
        django.setup()
    except Exception:
        print("[FATAL] django.setup() falló:\n")
        traceback.print_exc()
        input("\nPresioná ENTER para salir…")
        return

    # 3) migraciones “en vivo”
    try:
        from django.core.management import call_command
        print("[INFO] migrate …", flush=True)
        call_command("migrate", interactive=False, verbosity=1)
    except Exception:
        print("[ERROR] migrate falló:\n")
        traceback.print_exc()
        input("\nPresioná ENTER para salir…")
        return

    # 4) arrancar servidor
    print("="*70)
    print(f"Probar en esta PC:  http://127.0.0.1:{PORT}/")
    if BIND == "0.0.0.0":
        print(f"En la LAN:          http://{local_ip()}:{PORT}/")
    print("="*70, flush=True)

    try:
        if USE_WAITERSS:
            try:
                from tienda.wsgi import application
                import waitress
            except Exception:
                print("[ERROR] Falta waitress o wsgi. Usando runserver…")
                from django.core.management import call_command
                call_command("runserver", f"{BIND}:{PORT}", use_reloader=False)
                return

            print("[INFO] Levantando waitress…")
            waitress.serve(application, listen=f"{BIND}:{PORT}")
        else:
            print("[INFO] Levantando runserver (sin autoreload)…")
            call_command("runserver", f"{BIND}:{PORT}", use_reloader=False, verbosity=1)

    except Exception:
        print("[FATAL] Al iniciar el servidor:\n")
        traceback.print_exc()
        input("\nPresioná ENTER para salir…")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("\nPresioná ENTER para salir…")