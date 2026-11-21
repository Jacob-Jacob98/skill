"""Run inside the optional separator environment, with FFmpeg on PATH."""
import argparse
import json
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--models', type=Path, required=True)
    p.add_argument('--model', default='UVR-MDX-NET-Inst_HQ_3.onnx')
    a = p.parse_args()
    if Path(a.model).name != a.model or '/' in a.model or '\\' in a.model:
        p.error('--model must be a model filename, not a path or URL')
    from audio_separator.separator import Separator
    a.out.mkdir(parents=True, exist_ok=True)
    a.models.mkdir(parents=True, exist_ok=True)
    engine = Separator(model_file_dir=str(a.models.resolve()), output_dir=str(a.out.resolve()),
                       output_format='WAV', output_single_stem='Instrumental', use_soundfile=True)
    engine.load_model(model_filename=a.model)
    files = engine.separate(str(a.input.resolve()))
    paths = []
    for item in files:
        path = Path(item)
        path = path.resolve() if path.is_absolute() else (a.out/path).resolve()
        if path.is_relative_to(a.out.resolve()) and path.is_file():
            paths.append(str(path))
    if len(paths) != 1:
        raise RuntimeError('Expected exactly one instrumental output; inspect separation log')
    print(json.dumps({'instrumental': paths[0]}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
