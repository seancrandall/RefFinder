#!/usr/bin/env python3
"""patentbatch.py

Small-batch downloader for cited art (publications + grants) via USPTO ODP using uspto-odp.

Input: JSON from -i/--input or stdin, of the form:
{
  "cited_references": [
    {"type":"publication","number":"20240311942"},
    {"type":"grant","number":"11068477"}
  ]
}

Behavior:
- Validates JSON format.
- For each reference, resolves application serial number (ASN).
- Downloads an XML specification to <output_dir>/<number>.xml.
- If --drawings (or config drawings=true), attempts to download a TIFF to <number>.tiff when available.
- If a reference cannot be resolved (not found or ambiguous), creates an empty <number>.notfound and moves on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from uspto_odp.controller.uspto_odp_client import USPTOClient


MAX_BATCH = 100


class InputError(Exception):
    pass


@dataclass(frozen=True)
class CitedRef:
    ref_type: str  # "publication" | "grant"
    number: str


@dataclass
class Result:
    ref: CitedRef
    asn: Optional[str] = None
    xml_path: Optional[str] = None
    tiff_path: Optional[str] = None
    notfound_path: Optional[str] = None
    error: Optional[str] = None


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def _parse_bool(s: str) -> bool:
    s = (s or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def load_ini(path: Path) -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    if not path.exists():
        return cfg
    parser = ConfigParser()
    parser.read(path)
    if parser.has_section("odp"):
        sec = parser["odp"]
        for k in ("api_key", "api_keyfile", "drawings"):
            if k in sec and str(sec.get(k, "")).strip():
                cfg[k] = str(sec.get(k, "")).strip()
    return cfg


def read_api_key_from_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise InputError(f"Failed to read API keyfile: {path}: {exc}")


def resolve_api_key(args: argparse.Namespace, ini_cfg: Dict[str, str]) -> str:
    if args.odp_api_key and args.odp_api_key.strip():
        return args.odp_api_key.strip()

    keyfile = None
    if args.odp_api_keyfile:
        keyfile = Path(args.odp_api_keyfile)
    elif ini_cfg.get("api_keyfile"):
        keyfile = Path(ini_cfg["api_keyfile"])

    if keyfile is not None:
        return read_api_key_from_file(keyfile)

    if ini_cfg.get("api_key"):
        return ini_cfg["api_key"].strip()

    raise InputError(
        "No ODP API key provided. Use --odp-api-key, --odp-api-keyfile, or set [odp] api_key/api_keyfile in the ini."
    )


def resolve_drawings(args: argparse.Namespace, ini_cfg: Dict[str, str]) -> bool:
    if bool(args.drawings):
        return True
    if "drawings" in ini_cfg:
        return _parse_bool(ini_cfg["drawings"])
    return False


def load_input_json(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input:
        data = Path(args.input).read_text(encoding="utf-8")
    else:
        data = sys.stdin.read()

    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise InputError(f"Invalid JSON input: {exc}")

    if not isinstance(obj, dict):
        raise InputError("Input JSON must be an object.")

    if "cited_references" not in obj or not isinstance(obj["cited_references"], list):
        raise InputError("Input JSON must contain key 'cited_references' with an array value.")

    refs_raw = obj["cited_references"]
    if len(refs_raw) == 0:
        raise InputError("'cited_references' is empty.")
    if len(refs_raw) > MAX_BATCH:
        raise InputError(f"Too many references ({len(refs_raw)}). Hard limit is {MAX_BATCH}.")

    out: List[CitedRef] = []
    seen: set[Tuple[str, str]] = set()
    for i, r in enumerate(refs_raw):
        if not isinstance(r, dict):
            raise InputError(f"cited_references[{i}] must be an object.")
        t = r.get("type")
        n = r.get("number")
        if t not in {"publication", "grant"}:
            raise InputError(f"cited_references[{i}].type must be 'publication' or 'grant'.")
        if not isinstance(n, str) or not n.strip():
            raise InputError(f"cited_references[{i}].number must be a non-empty string.")
        n = n.strip()
        if not re.fullmatch(r"[0-9]+", n):
            raise InputError(f"cited_references[{i}].number must contain digits only.")

        key = (t, n)
        if key in seen:
            continue
        seen.add(key)
        out.append(CitedRef(ref_type=t, number=n))

    obj["_parsed_refs"] = out
    return obj


async def with_retries(coro_factory, *, what: str, attempts: int = 3) -> Any:
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            # backoff: 1, 2, 4 (+ jitter)
            delay = (2**i) + random.uniform(0.0, 0.25)
            eprint(f"WARN: {what} failed (attempt {i+1}/{attempts}): {exc}")
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _get_first_bag(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    bag = result.get("patentFileWrapperDataBag")
    if isinstance(bag, list) and bag:
        if isinstance(bag[0], dict):
            return bag[0]
    return None


def _extract_asns(result: Dict[str, Any]) -> List[str]:
    asns: List[str] = []
    bag = result.get("patentFileWrapperDataBag")
    if isinstance(bag, list):
        for item in bag:
            if isinstance(item, dict):
                asn = item.get("applicationNumberText")
                if isinstance(asn, str) and asn.strip():
                    asns.append(asn.strip())
    # unique preserve order
    seen: set[str] = set()
    uniq: List[str] = []
    for a in asns:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


async def resolve_asn_for_grant(client: USPTOClient, patent_no: str) -> Optional[str]:
    # Convenience method exists and is the most direct for grants.
    async def _call():
        return await client.get_app_metadata_from_patent_number(patent_no)

    try:
        meta = await with_retries(_call, what=f"get_app_metadata_from_patent_number({patent_no})")
    except Exception:
        meta = None

    # The meta object may be a dict-like or a pydantic model; be defensive.
    if meta is None:
        return None

    for attr in ("application_number", "applicationNumberText", "application_number_text"):
        val = getattr(meta, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()

    if isinstance(meta, dict):
        for k in ("applicationNumberText", "application_number", "application_number_text"):
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    # Fallback: search by patent number.
    async def _search():
        return await client.search_patent_applications_get(
            q=f"applicationMetaData.patentNumber:{patent_no}",
            fields="applicationNumberText,applicationMetaData.patentNumber",
            limit=5,
        )

    try:
        res = await with_retries(_search, what=f"search_patent_applications_get(patentNumber={patent_no})")
    except Exception:
        return None

    asns = _extract_asns(res if isinstance(res, dict) else {})
    if len(asns) == 1:
        return asns[0]
    return None


async def resolve_asn_for_publication(client: USPTOClient, pub_no: str) -> Optional[str]:
    # Try a couple query variants; take the first that yields a unique ASN.
    queries = [
        f"applicationMetaData.earliestPublicationNumber:{pub_no}",
        (
            f"(applicationMetaData.earliestPublicationNumber:{pub_no} OR "
            f"applicationMetaData.publicationNumber:{pub_no} OR "
            f"applicationMetaData.publicationNumberText:{pub_no})"
        ),
    ]

    for q in queries:
        async def _search():
            return await client.search_patent_applications_get(
                q=q,
                fields=(
                    "applicationNumberText,"
                    "applicationMetaData.earliestPublicationNumber,"
                    "applicationMetaData.publicationNumber,"
                    "applicationMetaData.publicationNumberText"
                ),
                limit=10,
            )

        try:
            res = await with_retries(_search, what=f"search_patent_applications_get(pub={pub_no})")
        except Exception:
            continue

        if not isinstance(res, dict):
            continue

        bag = res.get("patentFileWrapperDataBag")
        if not isinstance(bag, list) or not bag:
            continue

        # Prefer exact field matches when multiple hits exist.
        matches: List[Tuple[str, Dict[str, Any]]] = []
        for item in bag:
            if not isinstance(item, dict):
                continue
            asn = item.get("applicationNumberText")
            if not isinstance(asn, str) or not asn.strip():
                continue
            amd = item.get("applicationMetaData")
            candidates: List[str] = []
            if isinstance(amd, dict):
                for k in ("earliestPublicationNumber", "publicationNumber", "publicationNumberText"):
                    v = amd.get(k)
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())
            exact = any(c == pub_no for c in candidates)
            score = 2 if exact else 1
            matches.append((asn.strip(), {"score": score}))

        # Unique ASN?
        asns = [m[0] for m in matches]
        uniq_asns = []
        seen: set[str] = set()
        for a in asns:
            if a not in seen:
                seen.add(a)
                uniq_asns.append(a)

        if len(uniq_asns) == 1:
            return uniq_asns[0]

        # If multiple, see if exactly one has "exact" score.
        exact_asns = []
        for asn, meta in matches:
            if meta.get("score") == 2 and asn not in exact_asns:
                exact_asns.append(asn)
        if len(exact_asns) == 1:
            return exact_asns[0]

    return None


def choose_xml_document(documents_obj: Any) -> Optional[Any]:
    docs = getattr(documents_obj, "documents", None)
    if not isinstance(docs, list) or not docs:
        return None

    def has_xml(doc: Any) -> bool:
        opts = getattr(doc, "download_options", None)
        if not isinstance(opts, list):
            return False
        for opt in opts:
            mt = getattr(opt, "mime_type", None)
            if isinstance(mt, str) and mt.strip().upper() == "XML":
                return True
        return False

    def doc_text(doc: Any) -> str:
        parts = []
        for a in ("document_name", "documentCode", "document_code", "document_code_text", "document_code"):
            v = getattr(doc, a, None)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        v2 = getattr(doc, "document_name", None)
        if isinstance(v2, str) and v2.strip():
            parts.append(v2.strip())
        return " ".join(parts).lower()

    def doc_date(doc: Any) -> str:
        # Use sortable string; if missing, empty.
        for a in ("official_date", "officialDate", "mail_date", "mailDate"):
            v = getattr(doc, a, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    xml_docs = [d for d in docs if has_xml(d)]
    if not xml_docs:
        return None

    # Prefer "specification" in name; else any XML.
    spec_docs = [d for d in xml_docs if "specification" in doc_text(d) or "spec" in doc_text(d)]
    candidates = spec_docs if spec_docs else xml_docs

    # Prefer most recent official_date.
    candidates.sort(key=lambda d: doc_date(d), reverse=True)
    return candidates[0]


def choose_tiff_document(documents_obj: Any) -> Optional[Tuple[Any, str]]:
    docs = getattr(documents_obj, "documents", None)
    if not isinstance(docs, list) or not docs:
        return None

    def tiff_mime(opt: Any) -> Optional[str]:
        mt = getattr(opt, "mime_type", None)
        if not isinstance(mt, str) or not mt.strip():
            return None
        u = mt.strip().upper()
        if "TIF" in u or "TIFF" in u:
            return mt.strip()
        if u in {"IMAGE/TIFF", "IMAGE/TIF"}:
            return mt.strip()
        return None

    for doc in docs:
        opts = getattr(doc, "download_options", None)
        if not isinstance(opts, list):
            continue
        for opt in opts:
            mt = tiff_mime(opt)
            if mt:
                return (doc, mt)

    return None


async def process_one(
    client: USPTOClient,
    ref: CitedRef,
    out_dir: Path,
    want_drawings: bool,
) -> Result:
    res = Result(ref=ref)

    # Resolve ASN
    try:
        if ref.ref_type == "grant":
            asn = await resolve_asn_for_grant(client, ref.number)
        else:
            asn = await resolve_asn_for_publication(client, ref.number)

        if not asn:
            nf = out_dir / f"{ref.number}.notfound"
            nf.touch(exist_ok=True)
            res.notfound_path = str(nf)
            res.error = "not found"
            return res

        res.asn = asn

    except Exception as exc:
        nf = out_dir / f"{ref.number}.notfound"
        nf.touch(exist_ok=True)
        res.notfound_path = str(nf)
        res.error = f"resolve ASN failed: {exc}"
        return res

    # Documents
    try:
        async def _docs():
            return await client.get_patent_documents(res.asn)

        docs_obj = await with_retries(_docs, what=f"get_patent_documents({res.asn})")
    except Exception as exc:
        # Treat as a failure for this reference, but continue.
        nf = out_dir / f"{ref.number}.notfound"
        nf.touch(exist_ok=True)
        res.notfound_path = str(nf)
        res.error = f"get_patent_documents failed: {exc}"
        return res

    # XML doc selection
    doc = choose_xml_document(docs_obj)
    if not doc:
        nf = out_dir / f"{ref.number}.notfound"
        nf.touch(exist_ok=True)
        res.notfound_path = str(nf)
        res.error = "no XML download option found"
        return res

    # Download XML
    try:
        async def _dl_xml():
            return await client.download_document(document=doc, save_path=str(out_dir), mime_type="XML")

        file_path = await with_retries(_dl_xml, what=f"download_document(XML) {ref.number}")
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("download_document returned no path")

        src = Path(file_path)
        dst = out_dir / f"{ref.number}.xml"
        try:
            if src.resolve() != dst.resolve():
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
        except Exception:
            # If replace fails (cross-device), fall back to copy.
            import shutil

            shutil.copy2(src, dst)

        res.xml_path = str(dst)

    except Exception as exc:
        nf = out_dir / f"{ref.number}.notfound"
        nf.touch(exist_ok=True)
        res.notfound_path = str(nf)
        res.error = f"XML download failed: {exc}"
        return res

    # Optional drawings
    if want_drawings:
        try:
            pick = choose_tiff_document(docs_obj)
            if pick:
                tdoc, mt = pick

                async def _dl_tif():
                    return await client.download_document(document=tdoc, save_path=str(out_dir), mime_type=mt)

                tif_path = await with_retries(_dl_tif, what=f"download_document({mt}) {ref.number}")
                if isinstance(tif_path, str) and tif_path:
                    src = Path(tif_path)
                    dst = out_dir / f"{ref.number}.tiff"
                    try:
                        if src.resolve() != dst.resolve():
                            if dst.exists():
                                dst.unlink()
                            src.replace(dst)
                    except Exception:
                        import shutil

                        shutil.copy2(src, dst)
                    res.tiff_path = str(dst)
            else:
                # Not fatal.
                res.tiff_path = None
        except Exception as exc:
            # Not fatal; keep XML and report warning in error field.
            res.error = (res.error + "; " if res.error else "") + f"drawings not downloaded: {exc}"

    return res


async def main_async(args: argparse.Namespace) -> int:
    ini_cfg = load_ini(Path(args.config))
    api_key = resolve_api_key(args, ini_cfg)
    want_drawings = resolve_drawings(args, ini_cfg)

    obj = load_input_json(args)
    refs: List[CitedRef] = obj["_parsed_refs"]

    out_dir = Path(args.output_directory).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = USPTOClient(api_key=api_key)
    results: List[Result] = []

    try:
        for idx, ref in enumerate(refs, start=1):
            eprint(f"[{idx}/{len(refs)}] Processing {ref.ref_type}:{ref.number}")
            r = await process_one(client, ref, out_dir, want_drawings)
            results.append(r)
            if r.notfound_path:
                eprint(f"  -> NOTFOUND ({r.error})")
            else:
                eprint(f"  -> XML: {r.xml_path}")
                if want_drawings:
                    eprint(f"  -> TIFF: {r.tiff_path or 'n/a'}")

    finally:
        try:
            await client.session.close()
        except Exception:
            pass

    ok = sum(1 for r in results if r.xml_path)
    failed = sum(1 for r in results if r.notfound_path)

    summary = {
        "ok": ok,
        "failed": failed,
        "results": [
            {
                "type": r.ref.ref_type,
                "number": r.ref.number,
                "asn": r.asn,
                "xml": r.xml_path,
                "tiff": r.tiff_path,
                "notfound": r.notfound_path,
                "error": r.error,
            }
            for r in results
        ],
    }

    print(json.dumps(summary, indent=2, sort_keys=False))

    # Exit codes: 0 if all ok, 3 if partial failure.
    return 0 if failed == 0 else 3


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download cited ODP file wrapper XML (and optionally drawings) for small batches.")
    p.add_argument("-i", "--input", help="Path to JSON input file (otherwise reads from stdin)")
    p.add_argument(
        "-c",
        "--config",
        default="/data/models/share/patentbatch.ini",
        help="Path to ini config (default: /data/models/share/patentbatch.ini)",
    )
    p.add_argument("--odp-api-key", help="ODP API key (overrides keyfile)")
    p.add_argument("--odp-api-keyfile", help="Path to file containing ODP API key")
    p.add_argument("-d", "--drawings", action="store_true", help="Also download TIFF drawings when available")
    p.add_argument("-o", "--output-directory", default="./", help="Output directory (default: ./)")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        rc = asyncio.run(main_async(args))
    except InputError as exc:
        eprint(f"ERROR: {exc}")
        sys.exit(2)
    except KeyboardInterrupt:
        eprint("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        eprint(f"FATAL: {exc}")
        sys.exit(4)

    sys.exit(rc)


if __name__ == "__main__":
    main()

