# tienda.spec
import pathlib, os
from PyInstaller.utils.hooks import collect_submodules

base = pathlib.Path(".").resolve()

# --- Recolectar datos ---
datas = []
for folder in ["templates", "static", "media", "data", "shop", "tienda"]:
    p = base / folder
    if p.exists():
        datas.append((str(p), f"app/{folder}"))

# Archivos sueltos
for f in ["manage.py", "run_server.py"]:
    fp = base / f
    if fp.exists():
        datas.append((str(fp), "app"))

# Imports ocultos
hiddenimports = collect_submodules("django") + collect_submodules("mercadopago")

block_cipher = None

a = Analysis(
    ["run_server.py"],
    pathex=[str(base)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="tienda",
    console=True,  # dejalo en True así ves la consola
)
