"""Write a PyInstaller Windows version-resource file (shown in the exe's Properties > Details)."""
import re
import sys
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "j1939_engine_panel.py"
version = re.search(r'^__version__\s*=\s*"([^"]+)"', src.read_text(encoding="utf-8"), re.M).group(1)
nums = [int(x) for x in version.split("-")[0].split(".")] + [0, 0, 0, 0]
v4 = tuple(nums[:4])

out = Path(sys.argv[1] if len(sys.argv) > 1 else "version_info.txt")
out.write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={v4}, prodvers={v4}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'UncleAndy123'),
      StringStruct('FileDescription', 'J1939 Engine Panel'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'J1939EnginePanel'),
      StringStruct('LegalCopyright', 'GPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'J1939EnginePanel.exe'),
      StringStruct('ProductName', 'J1939 Engine Panel'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print(version)
