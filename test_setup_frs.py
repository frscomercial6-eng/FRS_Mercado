from pathlib import Path

iss_path = Path(__file__).resolve().parent / 'setup_frs.iss'
text = iss_path.read_text(encoding='utf-8')

assert 'WorkingDir: "{app}"' in text, 'Inno Setup shortcut must set WorkingDir to {app}'
assert 'Source: "dist\\FRS_Mercado\\*"' in text, 'Installer must copy the built bundle from dist\\FRS_Mercado'

print('setup_frs.iss validation passed')
