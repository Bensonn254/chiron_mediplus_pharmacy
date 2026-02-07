#!/usr/bin/env python3
"""
Localize images in a Medizo template (index-3.html) to Kenyan/Black people.

How it works:
- Parses the HTML, inspects each <img> tag and its surrounding section/context.
- Builds a context-aware search query.
- Uses a stock API (Pexels by default) to fetch a relevant photo.
- Backs up the original image files.
- Overwrites the original filenames so HTML does not need to change.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

TARGET_CATEGORIES = {"hero", "about", "doctor", "testimonial"}
SKIP_NAME_RE = re.compile(r"(logo|favicon|icon|shape|pattern|bg)", re.IGNORECASE)
SKIP_BG_NAME_RE = re.compile(r"(logo|favicon|icon|shape|pattern|overlay|wave|dot|line|map)", re.IGNORECASE)
SKIP_EXTS = {".svg"}
BG_ATTRS = ("data-background", "data-bg", "data-setbg")
MIN_SIZE_DEFAULT = 8 * 1024
MIN_SIZE_SMALL = 2 * 1024
CSS_ALLOW_HINTS = ("hero", "banner", "home-banner", "about", "doctor", "team", "testimonial", "testimonials")
CSS_DENY_HINTS = ("footer", "subscribe", "faq", "prescription", "brand", "shape", "pattern", "case", "blog", "serve", "product", "inner-banner", "emergency", "consultancy", "appointment")

SECTION_HINTS = (
    "hero",
    "banner",
    "slider",
    "about",
    "doctor",
    "team",
    "testimonial",
    "client",
    "patient",
)

CATEGORY_PRIORITY = {
    "hero": 4,
    "about": 3,
    "doctor": 2,
    "testimonial": 1,
    "generic": 0,
}


def build_arg_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    default_html = root / "templates.hibootstrap.com" / "medizo" / "default" / "index-3.html"
    parser = argparse.ArgumentParser(description="Localize Medizo images to Kenyan/Black imagery.")
    parser.add_argument(
        "--html",
        default=str(default_html),
        help="Path to index-3.html (default: templates.hibootstrap.com/medizo/default/index-3.html)",
    )
    parser.add_argument(
        "--dir",
        default="",
        help="Process all .html files in a directory (non-recursive). Overrides --html.",
    )
    parser.add_argument(
        "--provider",
        choices=["pexels", "unsplash"],
        default="pexels",
        help="Image source provider. Default: pexels",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of replacements (0 = no limit).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print actions without downloading or writing files.",
    )
    parser.add_argument(
        "--report",
        default="localize_images_report.json",
        help="Report JSON filename (saved next to HTML).",
    )
    return parser


def is_local_src(src: str) -> bool:
    if not src:
        return False
    return not (
        src.startswith(("http://", "https://", "//", "data:")) or src.startswith("#")
    )


def clean_src(src: str) -> str:
    return src.split("?")[0].split("#")[0].strip()

def extract_urls(value: str) -> List[str]:
    urls = []
    if not value:
        return urls
    for raw in re.findall(r"url\(([^)]+)\)", value, flags=re.IGNORECASE):
        url = raw.strip().strip("'\"")
        if url:
            urls.append(url)
    return urls


def find_section_tag(img) -> Optional[object]:
    if hasattr(img, "get"):
        classes = " ".join(img.get("class", [])).lower()
        ident = (img.get("id", "") or "").lower()
        if any(hint in classes or hint in ident for hint in SECTION_HINTS):
            return img
    for ancestor in img.parents:
        if not hasattr(ancestor, "get"):
            continue
        classes = " ".join(ancestor.get("class", [])).lower()
        ident = (ancestor.get("id", "") or "").lower()
        if any(hint in classes or hint in ident for hint in SECTION_HINTS):
            return ancestor
    return img.parent


def extract_nearby_text(section_tag) -> str:
    if not section_tag:
        return ""
    texts: List[str] = []
    for tag in section_tag.find_all(["h1", "h2", "h3", "h4", "h5", "p", "span", "li"], limit=16):
        text = tag.get_text(" ", strip=True)
        if text:
            texts.append(text)
    return " ".join(texts)

def element_context(el) -> Tuple[str, str]:
    section_tag = find_section_tag(el)
    section_label = ""
    if section_tag is not None and hasattr(section_tag, "get"):
        section_label = f"{section_tag.get('id','')} {' '.join(section_tag.get('class', []))}".strip()
    text = extract_nearby_text(section_tag)
    if hasattr(el, "get"):
        alt_text = el.get("alt", "")
        if alt_text:
            text = f"{alt_text} {text}".strip()
    if not text and hasattr(el, "get_text"):
        text = el.get_text(" ", strip=True)
    return section_label, text


def categorize(section_label: str, text: str) -> str:
    t = f"{section_label} {text}".lower()
    if any(k in t for k in ["hero", "banner", "slider"]):
        return "hero"
    if any(k in t for k in ["testimonial", "review", "client"]):
        return "testimonial"
    if any(k in t for k in ["doctor", "physician", "specialist", "surgeon", "pediatric", "cardio", "pharmacist"]):
        return "doctor"
    if any(k in t for k in ["about", "team", "staff"]):
        return "about"
    if "patient" in t:
        return "testimonial"
    return "generic"


def build_query(category: str, text: str, index: int, source: str = "") -> str:
    t = text.lower()
    if category == "hero":
        return "Modern Kenyan hospital interior with African doctors, photorealistic"
    if source.startswith("css") and category == "testimonial":
        return "Kenyan pharmacy customers smiling in pharmacy interior, wide shot"
    if category == "about":
        return "Kenyan medical team in pharmacy, diverse staff, professional"
    if category == "doctor":
        if "pediatric" in t:
            return "Black Kenyan female pediatrician smiling portrait in clinic"
        if "cardio" in t:
            return "Black Kenyan cardiologist in hospital setting"
        if "pharmac" in t:
            return "Black Kenyan pharmacist in pharmacy, professional portrait"
        variants = [
            "Black Kenyan female doctor headshot, clinic background",
            "Black Kenyan male doctor headshot, clinic background",
        ]
        return variants[index % len(variants)]
    if category == "testimonial":
        variants = [
            "Kenyan woman smiling portrait, patient testimonial",
            "Kenyan man smiling portrait, patient testimonial",
            "Kenyan older adult smiling portrait, patient testimonial",
        ]
        return variants[index % len(variants)]
    if "pharmac" in t:
        return "Black Kenyan pharmacist in pharmacy, professional portrait"
    return "Kenyan healthcare professional portrait, pharmacy setting"


def orientation_for(category: str, source: str = "") -> str:
    if source.startswith("css"):
        return "landscape"
    if category in {"hero", "about"}:
        return "landscape"
    return "portrait"


def query_variants(query: str) -> Iterable[str]:
    yield query
    if "Kenyan" in query:
        yield query.replace("Kenyan", "African")
        yield query.replace("Kenyan", "Black")
    yield re.sub(r"\bKenyan\b", "African", query)


def ensure_backup(img_path: Path, backup_root: Path) -> Path:
    rel = img_path.name
    backup_path = backup_root / rel
    if not backup_path.exists():
        shutil.copy2(img_path, backup_path)
    return backup_path

def is_under_assets(img_path: Path, assets_root: Path) -> bool:
    try:
        return assets_root == img_path or assets_root in img_path.parents
    except Exception:
        return False

def add_planned(planned: Dict[Path, Dict[str, object]], img_path: Path, meta: Dict[str, object]) -> None:
    existing = planned.get(img_path)
    if not existing:
        planned[img_path] = meta
        return

    existing_pages = existing.get("pages", [])
    meta_pages = meta.get("pages", [])
    combined_pages = sorted(set(existing_pages + meta_pages))
    existing["pages"] = combined_pages

    existing_priority = CATEGORY_PRIORITY.get(existing.get("category", ""), 0)
    new_priority = CATEGORY_PRIORITY.get(meta.get("category", ""), 0)
    if new_priority > existing_priority:
        meta["pages"] = combined_pages
        planned[img_path] = meta

def collect_plans_for_html(html_path: Path, planned: Dict[Path, Dict[str, object]]) -> None:
    html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html_text, "html.parser")
    imgs = soup.find_all("img")

    assets_root = (html_path.parent / "assets" / "img").resolve()
    if not assets_root.exists():
        print(f"[skip] Assets folder not found for {html_path}: {assets_root}")
        return

    for img in imgs:
        src = img.get("src", "")
        if not is_local_src(src):
            continue
        src_clean = clean_src(src)
        img_path = (html_path.parent / src_clean).resolve()
        if not img_path.exists():
            continue
        if img_path.suffix.lower() in SKIP_EXTS or SKIP_NAME_RE.search(img_path.name):
            continue
        if not is_under_assets(img_path, assets_root):
            continue

        section_label, text = element_context(img)
        category = categorize(section_label, text)
        if category not in TARGET_CATEGORIES:
            continue
        min_size = MIN_SIZE_SMALL if category == "testimonial" else MIN_SIZE_DEFAULT
        if img_path.stat().st_size < min_size:
            continue

        add_planned(
            planned,
            img_path,
            {
                "category": category,
                "text": text,
                "section": section_label,
                "source": "img",
                "pages": [str(html_path)],
            },
        )

    # Inline styles and data-background attributes
    for el in soup.find_all(True):
        urls: List[str] = []
        style = el.get("style", "")
        urls.extend(extract_urls(style))
        for attr in BG_ATTRS:
            attr_val = el.get(attr)
            if attr_val:
                urls.append(attr_val)

        for url in urls:
            if not is_local_src(url):
                continue
            url_clean = clean_src(url)
            img_path = (html_path.parent / url_clean).resolve()
            if not img_path.exists():
                continue
            if img_path.suffix.lower() in SKIP_EXTS or SKIP_BG_NAME_RE.search(img_path.name):
                continue
            if not is_under_assets(img_path, assets_root):
                continue
            section_label, text = element_context(el)
            category = categorize(section_label, text)
            if category not in TARGET_CATEGORIES:
                continue
            min_size = MIN_SIZE_SMALL if category == "testimonial" else MIN_SIZE_DEFAULT
            if img_path.stat().st_size < min_size:
                continue
            add_planned(
                planned,
                img_path,
                {
                    "category": category,
                    "text": text,
                    "section": section_label,
                    "source": "inline",
                    "pages": [str(html_path)],
                },
            )

    # External CSS background images
    css_paths = get_stylesheet_paths(soup, html_path)
    for css_path in css_paths:
        css_text = css_path.read_text(encoding="utf-8", errors="ignore")
        for selector, body in iter_css_blocks(css_text):
            if selector.lstrip().startswith("@"):
                continue
            if "url(" not in body:
                continue
            urls = extract_urls(body)
            if not urls:
                continue

            selectors = [s.strip() for s in selector.split(",") if s.strip()]
            for raw_sel in selectors:
                sel = sanitize_selector(raw_sel)
                if not sel or SKIP_BG_NAME_RE.search(sel):
                    continue
                try:
                    elements = soup.select(sel)
                except Exception:
                    elements = []
                if not elements:
                    continue

                section_label, text = element_context(elements[0])
                sel_text = f"{raw_sel} {section_label}".lower()
                if any(hint in sel_text for hint in CSS_DENY_HINTS):
                    continue
                if not any(hint in sel_text for hint in CSS_ALLOW_HINTS):
                    continue

                category = categorize(section_label, text)
                if category not in TARGET_CATEGORIES:
                    continue

                for url in urls:
                    if not is_local_src(url):
                        continue
                    url_clean = clean_src(url)
                    img_path = resolve_css_url(css_path, url_clean)
                    if not img_path.exists():
                        continue
                    if img_path.suffix.lower() in SKIP_EXTS or SKIP_BG_NAME_RE.search(img_path.name):
                        continue
                    if not is_under_assets(img_path, assets_root):
                        continue
                    min_size = MIN_SIZE_SMALL if category == "testimonial" else MIN_SIZE_DEFAULT
                    if img_path.stat().st_size < min_size:
                        continue

                    add_planned(
                        planned,
                        img_path,
                        {
                            "category": category,
                            "text": text,
                            "section": section_label,
                            "source": f"css:{css_path.name}",
                            "pages": [str(html_path)],
                        },
                    )

def sanitize_selector(selector: str) -> str:
    sel = selector.strip()
    sel = re.sub(r"::?[\w-]+", "", sel)
    sel = re.sub(r":(hover|active|focus|visited|link|before|after|nth-[^\\s]+)", "", sel)
    return sel.strip()

def iter_css_blocks(css_text: str) -> Iterable[Tuple[str, str]]:
    text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)

    def extract_blocks(s: str) -> List[Tuple[str, str]]:
        blocks: List[Tuple[str, str]] = []
        i = 0
        n = len(s)
        while i < n:
            if s.startswith("@media", i) or s.startswith("@supports", i):
                j = s.find("{", i)
                if j == -1:
                    break
                depth = 1
                k = j + 1
                while k < n and depth > 0:
                    if s[k] == "{":
                        depth += 1
                    elif s[k] == "}":
                        depth -= 1
                    k += 1
                inner = s[j + 1 : k - 1]
                blocks.extend(extract_blocks(inner))
                i = k
                continue

            j = s.find("{", i)
            if j == -1:
                break
            selector = s[i:j].strip()
            depth = 1
            k = j + 1
            while k < n and depth > 0:
                if s[k] == "{":
                    depth += 1
                elif s[k] == "}":
                    depth -= 1
                k += 1
            body = s[j + 1 : k - 1]
            if selector:
                blocks.append((selector, body))
            i = k
        return blocks

    return extract_blocks(text)

def get_stylesheet_paths(soup: BeautifulSoup, html_path: Path) -> List[Path]:
    paths: List[Path] = []
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v.lower()):
        href = link.get("href", "")
        if not is_local_src(href):
            continue
        href_clean = clean_src(href)
        css_path = (html_path.parent / href_clean).resolve()
        if css_path.exists():
            paths.append(css_path)
    return paths

def resolve_css_url(css_path: Path, url: str) -> Path:
    return (css_path.parent / url).resolve()


def fetch_pexels(query: str, orientation: str, api_key: str) -> Optional[str]:
    url = "https://api.pexels.com/v1/search"
    resp = requests.get(
        url,
        headers={"Authorization": api_key},
        params={"query": query, "per_page": 3, "orientation": orientation},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Pexels error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    photos = data.get("photos") or []
    if not photos:
        return None
    return photos[0].get("src", {}).get("large") or photos[0].get("src", {}).get("original")


def fetch_unsplash(query: str, orientation: str, api_key: str) -> Optional[str]:
    url = "https://api.unsplash.com/search/photos"
    resp = requests.get(
        url,
        headers={"Authorization": f"Client-ID {api_key}"},
        params={"query": query, "per_page": 3, "orientation": orientation},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Unsplash error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None
    return results[0].get("urls", {}).get("regular")


def download_image(url: str, dest: Path) -> None:
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with dest.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    html_paths: List[Path] = []
    if args.dir:
        dir_path = Path(args.dir).resolve()
        if not dir_path.exists():
            print(f"Directory not found: {dir_path}")
            return 1
        html_paths = sorted([p for p in dir_path.glob("*.html") if p.is_file()])
        if not html_paths:
            print(f"No .html files found in: {dir_path}")
            return 1
    else:
        html_path = Path(args.html).resolve()
        if not html_path.exists():
            print(f"HTML not found: {html_path}")
            return 1
        html_paths = [html_path]

    api_key = os.environ.get("PEXELS_API_KEY") if args.provider == "pexels" else os.environ.get("UNSPLASH_ACCESS_KEY")
    if not api_key:
        key_name = "PEXELS_API_KEY" if args.provider == "pexels" else "UNSPLASH_ACCESS_KEY"
        print(f"Missing API key: set {key_name} in your environment.")
        return 1

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    planned: Dict[Path, Dict[str, object]] = {}
    backup_root = None
    for html_path in html_paths:
        collect_plans_for_html(html_path, planned)
        if backup_root is None:
            assets_root = (html_path.parent / "assets" / "img").resolve()
            backup_root = (
                assets_root / f"_backup_site_{timestamp}"
                if args.dir
                else assets_root / f"_backup_{html_path.stem}_{timestamp}"
            )

    if not planned:
        print("No eligible images found for replacement.")
        return 0

    if not args.dry_run and backup_root is not None:
        backup_root.mkdir(parents=True, exist_ok=True)

    report: List[Dict[str, object]] = []
    count = 0
    for idx, (img_path, meta) in enumerate(planned.items()):
        category = meta["category"]
        text = meta["text"]
        query = build_query(category, text, idx, meta.get("source", ""))
        orientation = orientation_for(category, meta.get("source", ""))

        chosen_url = None
        for q in query_variants(query):
            if args.provider == "pexels":
                chosen_url = fetch_pexels(q, orientation, api_key)
            else:
                chosen_url = fetch_unsplash(q, orientation, api_key)
            if chosen_url:
                query = q
                break

        if not chosen_url:
            print(f"[skip] {img_path.name} -> no results for '{query}'")
            continue

        print(f"[replace] {img_path.name} <- {query}")
        if not args.dry_run:
            ensure_backup(img_path, backup_root)
            download_image(chosen_url, img_path)

        report.append(
            {
                "file": str(img_path),
                "category": category,
                "query": query,
                "image_url": chosen_url,
                "source": meta.get("source", ""),
                "section": meta.get("section", ""),
                "context_text": meta.get("text", ""),
                "pages": meta.get("pages", []),
            }
        )
        count += 1
        if args.limit and count >= args.limit:
            break

    report_base = Path(args.dir).resolve() if args.dir else html_paths[0].parent
    report_path = report_base / args.report
    if not args.dry_run:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report saved: {report_path}")
        print(f"Backups saved: {backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
