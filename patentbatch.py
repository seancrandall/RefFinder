#!/usr/bin/env python3

"""Batch-download USPTO file-wrapper documents (XML spec + optional TIFF drawings)
for a small set of cited references.

Input contract:
  JSON via -i/--input or stdin:
    {"cited_references": [{"type":"publication"|"grant","number":"..."}, ...]}

Behavior:
  - For each reference, resolve USPTO application serial number (ASN / applicationNumberText)
    via ODP search.
  - Then list documents for that application and download:
      * an XML specification -> <number>.xml
      * optionally drawings TIFF -> <number>.tiff (when --drawings)
  - If a reference cannot be resolved/fetched, do NOT exit:
      * write an empty <number>.notfound and continue.

Requires:
  pip package: uspto-odp
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from uspto_odp.controller.uspto_odp_client import USPTOClient
try:
    from uspto_odp.controller.uspto_odp_error import USPTOError
except ModuleNotFoundError:
    # Newer uspto-odp versions define USPTOError in uspto_odp_client.
    from uspto_odp.controller.uspto_odp_client import USPTOError


# ------------------------------
# Config
# ------------------------------
DEFAULT_CONFIG_PATH = "/data/models/share/patentbatch.ini"


@dataclass(frozen=True)
class CitedRef:
    ref_type: str  # "publication" or "grant"
    number: str


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_ini(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path)
    return cfg


def resolve_api_key(args: argparse.Namespace, cfg: configparser.ConfigParser) -> str:
    # 1) CLI override
    if args.odp_api_key:
        return args.odp_api_key.strip()

    # 2) CLI keyfile
    if args.odp_api_keyfile:
        return read_text_file(Path(args.odp_api_keyfile))

    # 3) INI direct key
    if cfg.has_option("odp", "api_key"):
        key = cfg.get("odp", "api_key").strip()
        if key:
            return key

    # 4) INI keyfile
    if cfg.has_option("odp", "api_keyfile"):
        kf = cfg.get("odp", "api_keyfile").strip()
        if kf:
            return read_text_file(Path(kf))

    # 5) env fallback
    env = os.getenv("ODP_API_KEY", "").strip()
    if env:
        return env

    raise SystemExit(
        "No ODP API key provided. Use --odp-api-key, --odp-api-keyfile, set in INI, or set ODP_API_KEY."
    )


# ------------------------------
# Input validation
# ------------------------------

def parse_input_json(data: Dict[str, Any]) -> List[CitedRef]:
    if not isinstance(data, dict):
        raise ValueError("Input JSON must be an object.")

    refs = data.get("cited_references")
    if not isinstance(refs, list):
        raise ValueError('Input JSON must contain key "cited_references" with a list value.')

    out: List[CitedRef] = []
    for i, item in enumerate(refs):
        if not isinstance(item, dict):
            raise ValueError(f"cited_references[{i}] must be an object.")

        t = item.get("type")
        n = item.get("number")
        if t not in ("publication", "grant"):
            raise ValueError(f'cited_references[{i}].type must be "publication" or "grant".')
        if not isinstance(n, str) or not n.strip():
            raise ValueError(f"cited_references[{i}].number must be a non-empty string.")

        # normalize: strip non-digits (user asked OA extractor to remove, but belt+suspenders)
        digits = re.sub(r"\D+", "", n)
        if not digits:
            raise ValueError(f"cited_references[{i}].number must contain at least one digit.")

        out.append(CitedRef(ref_type=t, number=digits))

    return out


def read_input(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as ex:
        raise SystemExit(f"Invalid JSON input: {ex}")


# ------------------------------
# ODP helpers
# ------------------------------

def _search_queries_for_ref(ref: CitedRef) -> List[str]:
    """Return a short list of q=... queries to try, from most to least specific.

    """
    n = ref.number
    if ref.ref_type == "grant":
        # Documented field example: applicationMetaData.patentNumber
        # (See ODP client docs examples.)
        return [
            f"applicationMetaData.patentNumber:{n}",
            f"applicationMetaData.patentNumberText:{n}",
            f"{n}",  # fallback free-text
        ]

    # publication
    return [
        f"applicationMetaData.publicationNumberText:{n}",
        f"applicationMetaData.publicationNumber:{n}",
        f"applicationMetaData.earliestPublicationNumber:{n}",
        f'"{n}"',  # exact phrase
        f"{n}",
    ]


def _extract_app_number(search_result: Dict[str, Any]) -> Optional[str]:
    bag = search_result.get("patentFileWrapperDataBag")
    if isinstance(bag, list) and bag:
        first = bag[0]
        if isinstance(first, dict):
            app = first.get("applicationNumberText")
            if isinstance(app, str) and app.strip():
                return app.strip()
    return None


async def resolve_application_number(
    client: USPTOClient, ref: CitedRef, verbose: bool = False
) -> Optional[str]:
    """Resolve a publication/grant number to applicationNumberText via ODP search."""
    for q in _search_queries_for_ref(ref):
        try:
            if verbose:
                eprint(f"INFO: {ref.ref_type} {ref.number}: search q={q!r}")
            # Keep the response small: only ask for the field we need.
            res = await client.search_patent_applications_get(
                q=q,
                limit=1,
                offset=0,
                fields="applicationNumberText",
            )
            app = _extract_app_number(res)
            if app:
                if verbose:
                    eprint(f"INFO: {ref.ref_type} {ref.number}: application number {app}")
                return app
        except USPTOError as e:
            # 400 means the field/query syntax is invalid for current schema; try the next one.
            if getattr(e, "code", None) in (400,):
                continue
            # Anything else (401/403/404/5xx): surface once and give up for this ref.
            eprint(f"WARN: search failed for {ref.ref_type} {ref.number} with q={q!r}: {e.code} {e.error}")
            return None
        except Exception as e:
            eprint(f"WARN: unexpected search error for {ref.ref_type} {ref.number} with q={q!r}: {e}")
            return None

    return None


def _pick_xml_spec(doc_bag: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose the best XML spec candidate.

    Strategy:
      - Prefer mimeType containing 'xml'
      - Prefer document codes that look like specification (SPEC) if present
      - Otherwise first xml-ish item

    Note: document-code conventions can vary; keep this conservative.
    """
    xmlish = [d for d in doc_bag if str(d.get("mimeType", "")).lower().find("xml") >= 0]
    if not xmlish:
        return None

    # Prefer SPEC-like document codes
    for d in xmlish:
        code = str(d.get("documentCode", "")).upper()
        if "SPEC" in code or code in {"SPEC", "SPE", "D-SPEC"}:
            return d

    return xmlish[0]


def _pick_tiff_drawings(doc_bag: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    tiffish = [d for d in doc_bag if str(d.get("mimeType", "")).lower() in {"image/tiff", "image/tif"}]
    if not tiffish:
        return None

    # Prefer drawings-ish document codes
    for d in tiffish:
        code = str(d.get("documentCode", "")).upper()
        if "DRW" in code or "DRAW" in code or code in {"DRW", "DRAW", "DR", "FIG"}:
            return d

    return tiffish[0]


async def download_doc(
    client: USPTOClient,
    app_number: str,
    doc: Dict[str, Any],
    out_path: Path,
) -> bool:
    """Download a single document using the ODP document download helper."""
    doc_id = doc.get("documentIdentifier") or doc.get("documentId")
    if not doc_id:
        return False

    try:
        # The library's download helper expects:
        #   download_document(app_number, document_identifier, destination_path)
        await client.download_document(app_number, str(doc_id), str(out_path))
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        eprint(f"WARN: download failed for app={app_number} doc={doc_id}: {e}")
        return False


async def process_one(
    client: USPTOClient,
    ref: CitedRef,
    out_dir: Path,
    want_drawings: bool,
    verbose: bool,
) -> None:
    num = ref.number
    notfound_path = out_dir / f"{num}.notfound"

    app_number = await resolve_application_number(client, ref, verbose=verbose)
    if not app_number:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (could not resolve application number)")
        return

    try:
        docs = await client.get_patent_documents(app_number)
    except Exception as e:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (could not list documents): {e}")
        return

    doc_bag = docs.get("documentBag") if isinstance(docs, dict) else None
    if not isinstance(doc_bag, list) or not doc_bag:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (empty document bag)")
        return

    xml_doc = _pick_xml_spec([d for d in doc_bag if isinstance(d, dict)])
    if not xml_doc:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (no XML-ish document)")
        return

    xml_path = out_dir / f"{num}.xml"
    ok_xml = await download_doc(client, app_number, xml_doc, xml_path)
    if not ok_xml:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (XML download failed)")
        return

    # Drawings
    if want_drawings:
        tiff_doc = _pick_tiff_drawings([d for d in doc_bag if isinstance(d, dict)])
        if tiff_doc:
            tiff_path = out_dir / f"{num}.tiff"
            ok_tiff = await download_doc(client, app_number, tiff_doc, tiff_path)
            if not ok_tiff:
                eprint(f"WARN: {ref.ref_type} {num}: drawings download failed; continuing")
        else:
            eprint(f"WARN: {ref.ref_type} {num}: no TIFF drawings found")

    eprint(f"OK: {ref.ref_type} {num} -> app {app_number}")


# ------------------------------
# Main
# ------------------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch-download cited art via USPTO ODP")
    p.add_argument("-i", "--input", help="Input JSON file (otherwise reads stdin)")
    p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"INI config path (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--odp-api-key", help="ODP API key (overrides keyfile)")
    p.add_argument("--odp-api-keyfile", help="Path to file containing ODP API key")
    p.add_argument("-d", "--drawings", action="store_true", help="Also download drawings TIFF")
    p.add_argument("-o", "--output-directory", default="./", help="Output directory")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    return p


async def async_main(args: argparse.Namespace) -> int:
    cfg = load_ini(Path(args.config))

    # INI default for drawings if CLI not specified
    ini_drawings = False
    if cfg.has_option("output", "drawings"):
        try:
            ini_drawings = cfg.getboolean("output", "drawings")
        except Exception:
            ini_drawings = False
    want_drawings = bool(args.drawings or ini_drawings)

    api_key = resolve_api_key(args, cfg)

    out_dir = Path(args.output_directory).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = read_input(args)
    try:
        refs = parse_input_json(raw)
    except ValueError as ex:
        raise SystemExit(f"Invalid input format: {ex}")

    # Hard cap: this is intended for small batches
    if len(refs) > 100:
        raise SystemExit(f"Refusing to process {len(refs)} references (cap is 100).")

    # IMPORTANT: do NOT override base_url.
    # The library already builds URLs like https://api.uspto.gov/api/v1/... internally.
    client = USPTOClient(api_key=api_key)

    try:
        for ref in refs:
            try:
                await process_one(client, ref, out_dir, want_drawings, bool(args.verbose))
            except Exception as e:
                # Per-reference failure must not abort the batch.
                (out_dir / f"{ref.number}.notfound").write_bytes(b"")
                eprint(f"WARN: {ref.ref_type} {ref.number}: unexpected error; marked notfound: {e}")
    finally:
        # Close the aiohttp session
        try:
            await client.session.close()
        except Exception:
            pass

    return 0


def main() -> int:
    args = build_argparser().parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())


# ==============================
# patentbatch.ini (example)
# ==============================
# Save this as /data/models/share/patentbatch.ini (or pass -c/--config)
#
# [odp]
# # Option A: put the key directly here
# api_key =
#
# # Option B: keep the key in a file (recommended)
# api_keyfile = /data/models/share/secrets/odp_api_key.txt
#
# [output]
# # default for drawings if -d/--drawings not provided
# drawings = false
