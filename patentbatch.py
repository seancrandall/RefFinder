#!/usr/bin/env python3

"""Batch-download USPTO file-wrapper documents (XML spec) for a small set of cited references.

Input contract:
  JSON via -i/--input or stdin:
    {"cited_references": [{"type":"publication"|"grant","number":"..."}, ...]}

Behavior:
  - For each reference, resolve USPTO application serial number via ODP search.
  - Then list documents for that application via ODP and download:
      * an XML specification archive -> extract to <number>.xml
  - Drawings download is not yet supported (flag is accepted but ignored).
  - If a reference cannot be resolved/fetched, do NOT exit:
      * write an empty <number>.notfound and continue.

Requires:
  aiohttp
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import aiohttp


# ------------------------------
# Config
# ------------------------------
DEFAULT_CONFIG_PATH = "/data/models/share/patentbatch.ini"
DEFAULT_BASE_URL = "https://api.uspto.gov"


LOG_TO_STDOUT = False


class ODPError(Exception):
    """Exception for USPTO ODP API errors."""

    def __init__(
        self,
        code: int,
        error: str,
        error_details: Optional[str] = None,
        request_identifier: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        self.code = code
        self.error = error
        self.error_details = error_details
        self.request_identifier = request_identifier
        self.url = url
        super().__init__(f"{code}: {error} - {error_details or 'No details provided'}")


class ODPClient:
    """Minimal async client for USPTO ODP endpoints."""

    def __init__(self, api_key: str, root_candidates: List[str]) -> None:
        self.root_candidates = root_candidates
        self.json_headers = {
            "accept": "application/json",
            "X-API-KEY": api_key,
            "User-Agent": "RefFinder/1.0",
        }
        # Match curl behavior for downloads: only send the API key.
        self.download_headers = {
            "X-API-KEY": api_key,
            "User-Agent": "RefFinder/1.0",
        }
        self.session = aiohttp.ClientSession()

    @staticmethod
    def _normalize_root(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/v1/patent/applications"):
            return base
        return f"{base}/v1/patent/applications"

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            pass

    async def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with self.session.get(url, params=params, headers=self.json_headers) as response:
            try:
                data = await response.json()
            except Exception:
                data = {}

            if response.status == 200:
                return data

            raise _error_from_response(url, response.status, data)

    async def search(self, base_url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        root = self._normalize_root(base_url)
        url = f"{root}/search"
        return await self._get_json(url, params=params)

    async def get_documents(self, base_url: str, app_number: str) -> Dict[str, Any]:
        root = self._normalize_root(base_url)
        url = f"{root}/{app_number}/documents"
        return await self._get_json(url)

    async def download_url(self, url: str, out_path: Path) -> bool:
        async with self.session.get(url, headers=self.download_headers, allow_redirects=True) as response:
            if response.status != 200:
                # Try to capture any structured error, but don't assume JSON.
                data: Dict[str, Any] = {}
                try:
                    data = await response.json()
                except Exception:
                    data = {}
                raise _error_from_response(url, response.status, data)

            with open(out_path, "wb") as f:
                while True:
                    chunk = await response.content.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
        return out_path.exists() and out_path.stat().st_size > 0


@dataclass(frozen=True)
class CitedRef:
    ref_type: str  # "publication" or "grant"
    number: str


def eprint(*args: Any, **kwargs: Any) -> None:
    target = sys.stdout if LOG_TO_STDOUT else sys.stderr
    print(*args, file=target, **kwargs)


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


def resolve_base_url(cfg: configparser.ConfigParser) -> str:
    # Optional override for the USPTO ODP base URL.
    if cfg.has_option("odp", "base_url"):
        base = cfg.get("odp", "base_url").strip()
        if base:
            return base

    env = os.getenv("ODP_BASE_URL", "").strip()
    if env:
        return env

    return ""


def _build_base_url_candidates(base_url: str) -> List[str]:
    base = base_url.rstrip("/")
    if not base:
        return []
    out = [base]
    if base.endswith("/api"):
        alt = base[:-4]
        if alt and alt not in out:
            out.append(alt)
    else:
        alt = f"{base}/api"
        if alt not in out:
            out.append(alt)
    return out


def resolve_api_root(cfg: configparser.ConfigParser) -> str:
    # Optional override for full API root (includes /v1/patent/applications).
    if cfg.has_option("odp", "api_root"):
        root = cfg.get("odp", "api_root").strip()
        if root:
            return root

    env = os.getenv("ODP_API_ROOT", "").strip()
    if env:
        return env

    return ""


def _error_from_response(url: str, status: int, data: Dict[str, Any]) -> ODPError:
    default_messages = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        429: "Too Many Requests",
        500: "Internal Server Error",
    }
    return ODPError(
        code=data.get("code", status),
        error=data.get("error", default_messages.get(status, "Unknown Error")),
        error_details=data.get("errorDetails") or data.get("errorDetailed"),
        request_identifier=data.get("requestIdentifier"),
        url=url,
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

def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _debug_dump_odp_search(label: str, payload: Dict[str, Any], verbose: bool) -> None:
    if not verbose:
        return
    try:
        dumped = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2)
    except Exception:
        dumped = str(payload)
    eprint(f"DEBUG: ODP search {label} payload:\n{dumped}")


def _build_odp_query(ref: CitedRef) -> str:
    if ref.ref_type == "publication":
        return f"US{ref.number}A1"
    return ref.number


def _extract_application_numbers(res: Dict[str, Any]) -> List[str]:
    bag = res.get("patentFileWrapperDataBag")
    if not isinstance(bag, list):
        return []
    out: List[str] = []
    for item in bag:
        if not isinstance(item, dict):
            continue
        app = item.get("applicationNumberText") or item.get("applicationNumber")
        if app is None:
            continue
        app_text = str(app).strip()
        if not app_text:
            continue
        out.append(app_text)
    return _dedupe_preserve_order(out)


def _select_application_number(candidates: List[str], ref: CitedRef) -> Optional[str]:
    if not candidates:
        return None
    if ref.ref_type == "grant":
        for candidate in candidates:
            if re.sub(r"\\D+", "", candidate) != ref.number:
                return candidate
    return candidates[0]


async def resolve_application_id(odp_client: ODPClient, ref: CitedRef, verbose: bool = False) -> Optional[str]:
    """Resolve a publication/grant number to application serial number via ODP search."""
    query = _build_odp_query(ref)
    last_error: Optional[Exception] = None
    for base_url in odp_client.root_candidates or [DEFAULT_BASE_URL]:
        try:
            if verbose and len(odp_client.root_candidates) > 1:
                eprint(f"INFO: using ODP base URL {base_url} for search")
            if verbose:
                eprint(f"INFO: {ref.ref_type} {ref.number}: search q={query}")
            res = await odp_client.search(base_url, params={"q": query})
            _debug_dump_odp_search(f"q={query}", res, verbose)
            candidates = _extract_application_numbers(res)
            app_id = _select_application_number(candidates, ref)
            if app_id:
                return app_id
            last_error = ValueError("no applicationNumberText in search results")
        except ODPError as e:
            last_error = e
            if getattr(e, "code", None) in (403, 404) and len(odp_client.root_candidates) > 1:
                continue
            break
        except Exception as e:
            last_error = e
            break

    if verbose and last_error:
        eprint(f"WARN: {ref.ref_type} {ref.number}: search failed: {last_error}")
    return None


def _mime_tokens(mime_value: Optional[str]) -> str:
    return str(mime_value or "").strip().upper()


def _find_download_option(doc: Dict[str, Any], tokens: Iterable[str]) -> Optional[Dict[str, Any]]:
    target = {t.upper() for t in tokens}
    for opt in doc.get("downloadOptionBag", []) or []:
        if not isinstance(opt, dict):
            continue
        mime = _mime_tokens(opt.get("mimeTypeIdentifier") or opt.get("mimeType"))
        if mime in target:
            return opt
    return None


def _pick_first_spec_xml(doc_bag: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose the first SPEC document that offers XML download.

    The documentBag is ordered newest-first, so the first match is the most recent.
    """
    for doc in doc_bag:
        if not isinstance(doc, dict):
            continue
        code = str(doc.get("documentCode", "")).upper()
        if code != "SPEC":
            continue
        if _find_download_option(doc, {"XML"}):
            return doc
    return None


def _extract_single_xml(archive_path: Path, out_path: Path) -> bool:
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            xml_infos = [
                info
                for info in zf.infolist()
                if info.filename
                and not info.filename.endswith("/")
                and info.filename.casefold().endswith(".xml")
            ]
            if not xml_infos:
                return False

            # Prefer the largest XML entry to avoid tiny metadata files.
            xml_infos.sort(key=lambda info: info.file_size, reverse=True)
            target = xml_infos[0]
            with zf.open(target) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return out_path.exists() and out_path.stat().st_size > 0
    except zipfile.BadZipFile:
        return False


async def download_doc(
    client: ODPClient,
    doc: Dict[str, Any],
    out_path: Path,
    mime_tokens: Iterable[str],
) -> bool:
    """Download a single document using its downloadUrl."""
    opt = _find_download_option(doc, mime_tokens)
    if not opt:
        return False
    url = opt.get("downloadUrl")
    if not url:
        return False

    try:
        return await client.download_url(str(url), out_path)
    except Exception as e:
        eprint(f"WARN: download failed for url={url}: {e}")
        return False


async def download_xml_archive(
    client: ODPClient,
    doc: Dict[str, Any],
    archive_path: Path,
    out_path: Path,
    verbose: bool = False,
    log_label: Optional[str] = None,
) -> bool:
    opt = _find_download_option(doc, {"XML"})
    if not opt:
        return False
    url = opt.get("downloadUrl")
    if not url:
        return False

    if verbose:
        label = f"{log_label} " if log_label else ""
        eprint(f"INFO: {label}downloadUrl: {url}")

    try:
        ok = await client.download_url(str(url), archive_path)
    except Exception as e:
        eprint(f"WARN: download failed for url={url}: {e}")
        return False
    if not ok:
        return False

    extracted = _extract_single_xml(archive_path, out_path)
    if extracted:
        try:
            archive_path.unlink()
        except FileNotFoundError:
            pass
        return True

    eprint(f"WARN: XML extraction failed for archive {archive_path}; keeping archive for inspection.")
    try:
        out_path.unlink()
    except FileNotFoundError:
        pass
    return False


async def process_one(
    odp_client: ODPClient,
    ref: CitedRef,
    out_dir: Path,
    verbose: bool,
) -> None:
    num = ref.number
    notfound_path = out_dir / f"{num}.notfound"

    app_id = await resolve_application_id(odp_client, ref, verbose=verbose)
    if not app_id:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (could not resolve application serial number)")
        return

    docs = None
    last_docs_error: Optional[Exception] = None
    for base_url in odp_client.root_candidates or [DEFAULT_BASE_URL]:
        try:
            if verbose and len(odp_client.root_candidates) > 1:
                eprint(f"INFO: using ODP base URL {base_url} for documents")
            docs = await odp_client.get_documents(base_url, app_id)
            break
        except ODPError as e:
            if getattr(e, "code", None) in (403, 404) and len(odp_client.root_candidates) > 1:
                last_docs_error = e
                continue
            last_docs_error = e
            break
        except Exception as e:
            last_docs_error = e
            break

    if not docs:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (could not list documents): {last_docs_error}")
        return

    doc_bag = docs.get("documentBag") if isinstance(docs, dict) else None
    if not isinstance(doc_bag, list) or not doc_bag:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (empty document bag)")
        return

    xml_doc = _pick_first_spec_xml([d for d in doc_bag if isinstance(d, dict)])
    if not xml_doc:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (no SPEC XML document)")
        return

    xml_path = out_dir / f"{num}.xml"
    archive_path = out_dir / f"{num}.xmlarchive"
    ok_xml = await download_xml_archive(
        odp_client,
        xml_doc,
        archive_path,
        xml_path,
        verbose=verbose,
        log_label=f"{ref.ref_type} {num} xmlarchive",
    )
    if not ok_xml:
        notfound_path.write_bytes(b"")
        eprint(f"INFO: {ref.ref_type} {num}: not found (XML archive extraction failed)")
        return

    eprint(f"OK: {ref.ref_type} {num} -> app {app_id}")


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
    p.add_argument("-d", "--drawings", action="store_true", help="Request drawings download (not yet supported)")
    p.add_argument("-o", "--output-directory", default="./", help="Output directory")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    return p


async def async_main(args: argparse.Namespace) -> int:
    global LOG_TO_STDOUT
    LOG_TO_STDOUT = bool(args.verbose)

    cfg = load_ini(Path(args.config))

    # INI default for drawings if CLI not specified
    ini_drawings = False
    for section in ("output", "odp"):
        if cfg.has_option(section, "drawings"):
            try:
                ini_drawings = cfg.getboolean(section, "drawings")
                break
            except Exception:
                ini_drawings = False
    want_drawings = bool(args.drawings or ini_drawings)
    if want_drawings:
        eprint("INFO: drawings download requested but not yet supported; skipping drawings.")

    api_key = resolve_api_key(args, cfg)
    api_root_override = resolve_api_root(cfg)
    base_override = resolve_base_url(cfg)

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

    if api_root_override:
        root_candidates = [api_root_override]
    else:
        base_candidates = _build_base_url_candidates(base_override or DEFAULT_BASE_URL)
        root_candidates = base_candidates or [DEFAULT_BASE_URL]

    client = ODPClient(api_key=api_key, root_candidates=root_candidates)

    try:
        for ref in refs:
            try:
                await process_one(client, ref, out_dir, bool(args.verbose))
            except Exception as e:
                # Per-reference failure must not abort the batch.
                (out_dir / f"{ref.number}.notfound").write_bytes(b"")
                eprint(f"WARN: {ref.ref_type} {ref.number}: unexpected error; marked notfound: {e}")
    finally:
        # Close the aiohttp session
        await client.close()

    return 0


def main() -> int:
    args = build_argparser().parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
