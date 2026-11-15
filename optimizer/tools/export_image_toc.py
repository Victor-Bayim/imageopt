#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import struct
import subprocess
import tarfile
import tempfile
from pathlib import Path


TOC_DIGEST_ANNOTATION = "containerd.io/snapshot/stargz/toc.digest"
EXTERNAL_LAYER_DIGEST_ANNOTATION = "containerd.io/snapshot/stargz/layer.digest"
TOC_TAR_NAME = "stargz.index.json"
EXTERNAL_TOC_MAGIC = b"STARGZEXTERNALTOC"
INLINE_TOC_MAGIC = b"STARGZ"
FOOTER_SIZE = 51
LEGACY_FOOTER_SIZE = 47
EXTERNAL_FOOTER_SIZE = 46


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _norm_path(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    return "/" + text.lstrip("/")


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_to_filename(digest: str) -> str:
    return digest.replace(":", "_")


class ExternalTOCRequired(RuntimeError):
    pass


class LiveResolver:
    def __init__(self, *, namespace: str) -> None:
        self.namespace = namespace or "default"

    def _run(self, argv: list[str], *, stdout=None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            text=stdout is None,
            capture_output=stdout is None,
            stdout=stdout,
            stderr=subprocess.PIPE,
            check=False,
        )

    def load_inspect_payload(self, image: str) -> dict:
        proc = self._run(["ctr", "-n", self.namespace, "images", "inspect", "--content", image])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"ctr images inspect failed for {image}")
        try:
            return json.loads(proc.stdout)
        except Exception as exc:
            raise RuntimeError(f"failed to parse ctr inspect payload for {image}: {exc}") from exc

    def load_json_by_digest(self, digest: str) -> dict:
        proc = self._run(["ctr", "-n", self.namespace, "content", "get", digest])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"ctr content get failed for {digest}")
        try:
            return json.loads(proc.stdout)
        except Exception as exc:
            raise RuntimeError(f"failed to parse content payload for {digest}: {exc}") from exc

    def copy_blob_to(self, digest: str, dest: Path) -> None:
        with dest.open("wb") as fh:
            proc = self._run(["ctr", "-n", self.namespace, "content", "get", digest], stdout=fh)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"ctr content get failed for {digest}")


class OfflineResolver:
    def __init__(self, *, manifest_json: Path, blob_dir: Path) -> None:
        self.manifest_json = manifest_json
        self.blob_dir = blob_dir

    def load_inspect_payload(self, image: str) -> dict:
        del image
        return json.loads(self.manifest_json.read_text(encoding="utf-8"))

    def load_json_by_digest(self, digest: str) -> dict:
        path = self.blob_dir / _digest_to_filename(digest)
        return json.loads(path.read_text(encoding="utf-8"))

    def copy_blob_to(self, digest: str, dest: Path) -> None:
        src = self.blob_dir / _digest_to_filename(digest)
        shutil.copyfile(src, dest)


def _normalize_arch(arch: str) -> str:
    mapping = {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }
    return mapping.get(arch, arch)


def _select_manifest_descriptor(manifests: list[dict]) -> dict:
    host_os = "linux"
    host_arch = _normalize_arch(platform.machine().lower())
    for desc in manifests:
        plat = desc.get("platform") or {}
        if plat.get("os") == host_os and _normalize_arch(str(plat.get("architecture") or "").lower()) == host_arch:
            return desc
    for desc in manifests:
        plat = desc.get("platform") or {}
        if plat.get("os") == host_os:
            return desc
    return manifests[0]


def resolve_manifest_json(root: dict, resolver) -> tuple[dict, str | None]:
    current = root
    manifest_digest: str | None = None
    for _ in range(4):
        if isinstance(current, dict) and isinstance(current.get("layers"), list):
            return current, manifest_digest
        manifests = current.get("manifests")
        if isinstance(manifests, list) and manifests:
            selected = _select_manifest_descriptor([desc for desc in manifests if isinstance(desc, dict)])
            manifest_digest = str(selected.get("digest") or manifest_digest or "")
            current = resolver.load_json_by_digest(manifest_digest)
            continue
        target = current.get("target")
        if isinstance(target, dict) and target.get("digest"):
            manifest_digest = str(target.get("digest"))
            current = resolver.load_json_by_digest(manifest_digest)
            continue
        manifest = current.get("manifest")
        if isinstance(manifest, dict):
            current = manifest
            continue
        break
    raise RuntimeError("unable to resolve OCI manifest with layers from image metadata")


def parse_footer(footer: bytes) -> tuple[str, int]:
    if len(footer) < 18:
        raise ValueError("footer too short")
    if footer[0:3] != b"\x1f\x8b\x08":
        raise ValueError("footer is not gzip")
    if not (footer[3] & 0x04):
        raise ValueError("footer does not contain gzip extra field")
    xlen = struct.unpack("<H", footer[10:12])[0]
    extra = footer[12 : 12 + xlen]
    if len(extra) < 4:
        raise ValueError("invalid gzip extra field")
    if extra[0:2] == b"SG" and len(extra) >= 4:
        subfield_len = struct.unpack("<H", extra[2:4])[0]
        subfield = extra[4 : 4 + subfield_len]
        if len(subfield) != subfield_len:
            raise ValueError("truncated gzip extra subfield")
        if subfield.endswith(INLINE_TOC_MAGIC):
            return ("inline", int(subfield[:16], 16))
        if subfield.endswith(EXTERNAL_TOC_MAGIC):
            return ("external", 0)
        raise ValueError("unsupported stargz footer magic")
    if extra.endswith(INLINE_TOC_MAGIC):
        return ("inline", int(extra[:16], 16))
    raise ValueError("unknown stargz footer layout")


def extract_inline_toc_from_blob(blob_path: Path) -> tuple[dict, str]:
    size = blob_path.stat().st_size
    errors: list[str] = []
    with blob_path.open("rb") as fh:
        for footer_size in (FOOTER_SIZE, LEGACY_FOOTER_SIZE, EXTERNAL_FOOTER_SIZE):
            if size < footer_size:
                continue
            fh.seek(size - footer_size)
            footer = fh.read(footer_size)
            try:
                kind, toc_offset = parse_footer(footer)
            except Exception as exc:
                errors.append(str(exc))
                continue
            if kind == "external":
                raise ExternalTOCRequired("layer uses external TOC footer")
            fh.seek(toc_offset)
            with gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                toc_bytes = gz.read()
            try:
                return json.loads(toc_bytes), _hash_bytes(toc_bytes)
            except Exception as exc:
                raise RuntimeError(f"failed to decode TOC JSON: {exc}") from exc
    raise RuntimeError("; ".join(errors) or "failed to locate inline TOC footer")


def extract_external_toc_from_blob(blob_path: Path) -> tuple[dict, str]:
    with gzip.open(blob_path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r:") as tf:
            for member in tf:
                if member.name != TOC_TAR_NAME:
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    break
                toc_bytes = fh.read()
                return json.loads(toc_bytes), _hash_bytes(toc_bytes)
    raise RuntimeError("external TOC tar does not contain stargz.index.json")


def normalize_toc_entries(*, layer_digest: str, toc_json: dict, toc_digest: str) -> list[dict]:
    entries_by_path: dict[str, dict] = {}
    order: list[str] = []
    for raw in list(toc_json.get("entries") or []):
        if not isinstance(raw, dict):
            continue
        entry_type = str(raw.get("type") or "")
        if entry_type not in {"reg", "chunk"}:
            continue
        path = _norm_path(raw.get("name"))
        if not path or path.endswith("/stargz.index.json"):
            continue
        size_bytes = _safe_int(raw.get("size"))
        offset = _safe_int(raw.get("offset"))
        chunk_offset = _safe_int(raw.get("chunkOffset"))
        chunk_size = _safe_int(raw.get("chunkSize"))
        inner_offset = _safe_int(raw.get("innerOffset"))
        if path not in entries_by_path:
            entries_by_path[path] = {
                "path": path,
                "layer_digest": layer_digest,
                "toc_digest": toc_digest,
                "size_bytes": max(size_bytes, 0),
                "chunks": [],
            }
            order.append(path)
        normalized = entries_by_path[path]
        if size_bytes > int(normalized.get("size_bytes") or 0):
            normalized["size_bytes"] = size_bytes
        effective_chunk_size = chunk_size
        if effective_chunk_size <= 0 and size_bytes > chunk_offset:
            effective_chunk_size = size_bytes - chunk_offset
        if effective_chunk_size <= 0 and int(normalized.get("size_bytes") or 0) > chunk_offset:
            effective_chunk_size = int(normalized["size_bytes"]) - chunk_offset
        if effective_chunk_size <= 0:
            continue
        normalized["chunks"].append(
            {
                "offset": offset,
                "chunk_offset": chunk_offset,
                "size": effective_chunk_size,
                "compressed_size": effective_chunk_size,
                "inner_offset": inner_offset,
                "chunk_digest": raw.get("chunkDigest"),
            }
        )
    return [entries_by_path[path] for path in order]


def derive_external_toc_image_ref(image: str) -> str:
    return f"{image}-esgztoc"


def build_external_toc_index(
    *,
    resolver,
    root_payload: dict,
) -> dict[str, dict]:
    toc_manifest_json, _ = resolve_manifest_json(root_payload, resolver)
    index: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="cpp_sampler_toc_external_") as tmpdir:
        tmp_root = Path(tmpdir)
        for layer in list(toc_manifest_json.get("layers") or []):
            if not isinstance(layer, dict):
                continue
            annotations = dict(layer.get("annotations") or {})
            source_layer_digest = str(annotations.get(EXTERNAL_LAYER_DIGEST_ANNOTATION) or "")
            toc_layer_digest = str(layer.get("digest") or "")
            if not source_layer_digest or not toc_layer_digest:
                continue
            blob_path = tmp_root / f"external_{len(index)}.blob"
            resolver.copy_blob_to(toc_layer_digest, blob_path)
            toc_json, computed_toc_digest = extract_external_toc_from_blob(blob_path)
            index[source_layer_digest] = {
                "toc_json": toc_json,
                "toc_digest": computed_toc_digest,
                "toc_layer_digest": toc_layer_digest,
            }
    return index


def build_image_toc_payload(
    *,
    image: str | None,
    manifest_json: dict,
    manifest_digest: str | None,
    resolver,
    external_toc_root: dict | None = None,
    external_toc_image_ref: str | None = None,
) -> dict:
    layers_payload: list[dict] = []
    entries_payload: list[dict] = []
    warnings: list[str] = []
    external_toc_index: dict[str, dict] | None = None
    external_toc_error: str | None = None

    def get_external_toc_index() -> dict[str, dict]:
        nonlocal external_toc_index, external_toc_error, external_toc_root
        if external_toc_index is not None:
            return external_toc_index
        if external_toc_error is not None:
            raise RuntimeError(external_toc_error)
        if external_toc_root is None:
            if not external_toc_image_ref:
                external_toc_error = "no external TOC image is configured"
                raise RuntimeError(external_toc_error)
            try:
                external_toc_root = resolver.load_inspect_payload(external_toc_image_ref)
            except Exception as exc:
                external_toc_error = f"failed to inspect external TOC image {external_toc_image_ref}: {exc}"
                raise RuntimeError(external_toc_error) from exc
        try:
            external_toc_index = build_external_toc_index(resolver=resolver, root_payload=external_toc_root)
        except Exception as exc:
            external_toc_error = str(exc)
            raise RuntimeError(external_toc_error) from exc
        if not external_toc_index:
            external_toc_error = "external TOC image did not contain any stargz.index.json layer mappings"
            raise RuntimeError(external_toc_error)
        return external_toc_index

    with tempfile.TemporaryDirectory(prefix="cpp_sampler_toc_") as tmpdir:
        tmp_root = Path(tmpdir)
        for index, layer in enumerate(list(manifest_json.get("layers") or [])):
            if not isinstance(layer, dict):
                continue
            layer_digest = str(layer.get("digest") or "")
            annotations = dict(layer.get("annotations") or {})
            expected_toc_digest = str(annotations.get(TOC_DIGEST_ANNOTATION) or "")
            if not layer_digest:
                warnings.append(f"layer {index} is missing digest")
                continue
            blob_path = tmp_root / f"layer_{index}.blob"
            try:
                resolver.copy_blob_to(layer_digest, blob_path)
                toc_json, computed_toc_digest = extract_inline_toc_from_blob(blob_path)
                toc_source = "inline"
                toc_layer_digest = None
            except ExternalTOCRequired:
                try:
                    record = get_external_toc_index().get(layer_digest)
                except Exception as exc:
                    warnings.append(f"{layer_digest}: external TOC required but unavailable: {exc}")
                    continue
                if record is None:
                    warnings.append(f"{layer_digest}: external TOC mapping not found in TOC image")
                    continue
                toc_json = record["toc_json"]
                computed_toc_digest = record["toc_digest"]
                toc_source = "external"
                toc_layer_digest = record.get("toc_layer_digest")
            except Exception as exc:
                warnings.append(f"{layer_digest}: {exc}")
                continue
            entries = normalize_toc_entries(
                layer_digest=layer_digest,
                toc_json=toc_json,
                toc_digest=computed_toc_digest,
            )
            digest_match = not expected_toc_digest or expected_toc_digest == computed_toc_digest
            layers_payload.append(
                {
                    "digest": layer_digest,
                    "toc_source": toc_source,
                    "toc_layer_digest": toc_layer_digest,
                    "toc_digest_annotation": expected_toc_digest or None,
                    "toc_digest_computed": computed_toc_digest,
                    "toc_digest_verified": digest_match,
                    "entry_count": len(entries),
                }
            )
            entries_payload.extend(entries)
            if expected_toc_digest and not digest_match:
                warnings.append(
                    f"{layer_digest}: TOC digest annotation mismatch (want {expected_toc_digest}, got {computed_toc_digest})"
                )
    if not layers_payload:
        raise RuntimeError("no eStargz TOC could be extracted from image layers")
    return {
        "format_version": 1,
        "image": image,
        "manifest_digest": manifest_digest,
        "toc_digest_annotation_key": TOC_DIGEST_ANNOTATION,
        "layers": layers_payload,
        "entries": entries_payload,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export inline/external eStargz TOC into image_toc.json.")
    parser.add_argument("--image", help="image reference available in local containerd")
    parser.add_argument("--toc-image", help="optional external TOC image reference (defaults to <image>-esgztoc)")
    parser.add_argument("--namespace", default="default", help="containerd namespace")
    parser.add_argument("--manifest-json", help="offline manifest/index JSON path")
    parser.add_argument("--toc-manifest-json", help="offline external TOC manifest/index JSON path")
    parser.add_argument("--blob-dir", help="offline blob directory keyed by digest.replace(':', '_')")
    parser.add_argument("--out", required=True, help="path to normalized image_toc.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_path = Path(args.out)
    if args.manifest_json:
        if not args.blob_dir:
            raise SystemExit("--blob-dir is required with --manifest-json")
        resolver = OfflineResolver(
            manifest_json=Path(args.manifest_json),
            blob_dir=Path(args.blob_dir),
        )
        inspect_payload = resolver.load_inspect_payload("")
        image = None
        external_toc_root = (
            json.loads(Path(args.toc_manifest_json).read_text(encoding="utf-8"))
            if args.toc_manifest_json
            else None
        )
        external_toc_image_ref = None
    else:
        if not args.image:
            raise SystemExit("--image is required unless --manifest-json is provided")
        resolver = LiveResolver(namespace=args.namespace)
        inspect_payload = resolver.load_inspect_payload(args.image)
        image = args.image
        external_toc_root = None
        external_toc_image_ref = args.toc_image or derive_external_toc_image_ref(args.image)

    manifest_json, manifest_digest = resolve_manifest_json(inspect_payload, resolver)
    payload = build_image_toc_payload(
        image=image,
        manifest_json=manifest_json,
        manifest_digest=manifest_digest,
        resolver=resolver,
        external_toc_root=external_toc_root,
        external_toc_image_ref=external_toc_image_ref,
    )
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
