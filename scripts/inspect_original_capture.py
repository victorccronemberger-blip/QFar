"""Explicit read-only capture inspection; output excludes source metadata."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _inspector():
    # Load the pure utility directly; legacy moneymin package initialization
    # must not cause configuration, vault, generator or provider activity.
    name = "_qmoney_read_only_capture_import"
    path = Path(__file__).resolve().parents[1] / "moneymin/capture_import.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Não foi possível carregar o inspetor local.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspeciona originais explicitamente indicados, sem copiar, alterar ou enviar.")
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="Resumo mínimo em JSON no stdout.")
    args = parser.parse_args(argv)
    inspector = _inspector()
    try:
        descriptor = inspector.inspect_original_capture(args.media, args.sidecar)
    except inspector.CaptureImportError as error:
        summary = {"inspection": "original_capture_read_only_v1", "valid_structure": False,
                   "validation_policy": "safety_container_identity_csv_types",
                   "error_code": error.code,
                   "physical_provenance_verified": False, "provider_acceptance_verified": False}
        if args.json:
            print(json.dumps(summary, separators=(",", ":")))
        else:
            print(str(error), file=sys.stderr)
        return 2
    summary = descriptor.public_summary()
    if args.json:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print("Inspeção estrutural somente leitura concluída.")
        print(f"Media: {descriptor.media.bytes} bytes; SHA256 {descriptor.media.sha256}")
        print(f"Sidecar: {descriptor.sidecar.bytes} bytes; SHA256 {descriptor.sidecar.sha256}")
        print(f"Membros: {len(descriptor.members)}; linhas IMU: {descriptor.imu_rows}; quadros: {descriptor.frames_rows}")
        print("Origem física e aceitação pelo provedor continuam sem comprovação.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
