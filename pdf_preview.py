"""Convert a PDF into a small, standalone text-first HTML reader.

Text mode (default) omits images, fonts, and precise positioning to minimize size.
Use --mode image for compressed page images with a selectable text overlay.
No browser extension, internet connection, or original PDF is needed to read it.
Scanned pages need prior OCR for text mode; use image mode otherwise.
"""

import argparse
import base64
from html import escape
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import pymupdf


CSS = r"""
:root{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#243249;background:#edf1f6;--bar:64px}
*{box-sizing:border-box}body{margin:0}button,input,select{font:inherit}button,select,input{border:1px solid #ccd5e1;border-radius:7px;background:white;padding:7px 10px;color:inherit}button,select{cursor:pointer}button:hover{background:#eaf0f9}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #2563eb;outline-offset:2px}
.toolbar{height:var(--bar);position:fixed;inset:0 0 auto;z-index:10;display:flex;align-items:center;gap:9px;padding:10px 18px;background:#ffffffed;border-bottom:1px solid #d9e0e9;backdrop-filter:blur(10px)}h1{font-size:15px;margin:0 auto 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:32vw}.page-input{width:64px}.total{white-space:nowrap;color:#627187;font-size:13px}.search{width:160px}.search-status{font-size:12px;white-space:nowrap;min-width:45px}
.sidebar{position:fixed;inset:var(--bar) auto 0 0;width:245px;overflow:auto;background:#f8fafc;border-right:1px solid #d9e0e9;padding:18px 12px}.sidebar h2{font-size:11px;letter-spacing:.12em;color:#718096;margin:0 9px 16px}.sidebar a{display:block;text-decoration:none;font-size:13px;line-height:1.5;color:#52617a;padding:7px 9px;border-radius:6px}.sidebar a:hover,.sidebar a.active{background:#e3ecfa;color:#234f9d}
main{margin-left:245px;padding:calc(var(--bar) + 26px) 24px 40px;overflow-x:auto;min-height:100vh}.page-frame{position:relative;margin:0 auto 26px;scroll-margin-top:calc(var(--bar) + 20px);box-shadow:0 3px 18px #1f36531c;background:white}.page-inner{position:relative;transform-origin:top left}.paper{display:block;position:absolute;inset:0;width:100%;height:100%;user-select:none;pointer-events:none}.text-layer{position:absolute;inset:0;overflow:hidden;color:transparent}.text-layer span{position:absolute;white-space:pre;line-height:1;font-family:sans-serif;transform-origin:top left;cursor:text}.text-layer ::selection{background:#4b8ff65c;color:transparent}.pdf-link{position:absolute;z-index:2}.pdf-link:hover{background:#3b82f621;outline:1px solid #3b82f655}.search-hit{outline:3px solid #6296ef;outline-offset:3px}.page-label{position:absolute;left:0;top:-19px;color:#718096;font-size:11px;pointer-events:none}.no-sidebar .sidebar{display:none}.no-sidebar main{margin-left:0}
.text-mode .page-frame{max-width:1100px;width:100%;height:auto}.text-mode .page-inner{padding:24px 28px}.report-text{margin:0;white-space:pre;overflow-x:auto;font-family:ui-monospace,"SFMono-Regular",Consolas,"Noto Sans Mono CJK SC",monospace;font-size:var(--text-size,14px);line-height:1.7;color:#273449}.text-mode .page-label{letter-spacing:.03em}
@media(max-width:1000px){h1{display:none}.toolbar{padding:8px;gap:5px}.search{width:115px}.sidebar{width:210px}main{margin-left:210px;padding-left:14px;padding-right:14px}}
@media(max-width:700px){.sidebar{display:none}main{margin-left:0}.search{width:90px}.search-status{display:none}.toolbar{overflow-x:auto}.toolbar button{padding:7px}.total{font-size:12px}}
@media print{.toolbar,.sidebar,.page-label,.pdf-link{display:none!important}body{background:white}main{margin:0;padding:0;overflow:visible}.page-frame{box-shadow:none;outline:none;margin:0 auto;break-after:page;width:100%!important;height:auto!important}.page-inner{width:100%!important;height:auto!important;transform:none!important}.paper{position:static;width:100%;height:auto;print-color-adjust:exact}.text-layer{display:none}.report-text{white-space:pre-wrap;overflow:visible;font-size:9pt}}
"""

JS = r"""
const data=JSON.parse(document.getElementById('report-data').textContent);
const frames=[...document.querySelectorAll('.page-frame')], main=document.querySelector('main');
const pageInput=document.getElementById('page-number'), zoom=document.getElementById('zoom');
let current=data.first, query='', matches=[], matchIndex=-1;
const canvas=document.createElement('canvas'), context=canvas.getContext('2d');
document.querySelectorAll('.text-layer span').forEach(span=>{
 context.font=getComputedStyle(span).font;
 const width=context.measureText(span.textContent).width;
 if(width)span.style.transform=`scaleX(${Number(span.dataset.width)/width})`;
});
function applyZoom(){
 if(data.mode==='text'){main.style.setProperty('--text-size',`${14*(zoom.value==='fit'?1:Number(zoom.value))}px`);return;}
 const scale=zoom.value==='fit'?Math.max(.1,Math.min(3,(main.clientWidth-48)/Math.max(...data.pages.map(p=>p.width)))):Number(zoom.value);
 frames.forEach(frame=>{
  frame.style.width=`${Number(frame.dataset.width)*scale}px`;
  frame.style.height=`${Number(frame.dataset.height)*scale}px`;
  frame.querySelector('.page-inner').style.transform=`scale(${scale})`;
 });
}
function jump(number){
 const frame=document.getElementById(`page-${number}`);
 if(!frame)return;
 frame.scrollIntoView({block:'start'});current=number;pageInput.value=number;
}
document.getElementById('previous').onclick=()=>jump(Math.max(data.first,current-1));
document.getElementById('next').onclick=()=>jump(Math.min(data.last,current+1));
pageInput.onchange=()=>{const n=Number(pageInput.value);if(Number.isInteger(n)&&n>=data.first&&n<=data.last)jump(n);else pageInput.value=current;};
zoom.onchange=applyZoom;window.addEventListener('resize',()=>{if(zoom.value==='fit')applyZoom();});
document.getElementById('toggle-nav').onclick=()=>{document.body.classList.toggle('no-sidebar');applyZoom();};
document.getElementById('print').onclick=()=>window.print();
document.getElementById('search-form').onsubmit=event=>{
 event.preventDefault();const nextQuery=document.getElementById('search').value.trim().toLocaleLowerCase();
 if(nextQuery!==query){query=nextQuery;matchIndex=-1;matches=query?data.pages.filter(p=>(p.text??document.querySelector(`#page-${p.number} .report-text`).textContent).toLocaleLowerCase().includes(query)):[];}
 document.querySelectorAll('.search-hit').forEach(p=>p.classList.remove('search-hit'));
 if(matches.length){matchIndex=(matchIndex+1)%matches.length;jump(matches[matchIndex].number);document.getElementById(`page-${matches[matchIndex].number}`).classList.add('search-hit');}
 document.getElementById('search-status').textContent=matches.length?`${matchIndex+1}/${matches.length} pages`:(query?'No results':'');
};
let scheduled=false;
window.addEventListener('scroll',()=>{
 if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{
  const frame=frames.find(p=>p.getBoundingClientRect().bottom>100);
  if(frame){current=Number(frame.dataset.number);pageInput.value=current;}
  const links=[...document.querySelectorAll('.sidebar a')];let active=null;
  links.forEach(a=>{a.classList.remove('active');if(Number(a.dataset.page)<=current)active=a;});
  if(active)active.classList.add('active');scheduled=false;
 });
},{passive:true});
applyZoom();
if(location.hash.startsWith('#page-')){const number=Number(location.hash.slice(6));jump(number);}
"""


def _position(rect: pymupdf.Rect) -> str:
    return f"left:{rect.x0:.3f}px;top:{rect.y0:.3f}px;width:{rect.width:.3f}px;height:{rect.height:.3f}px"


def render_page(page: pymupdf.Page, number: int, first: int, last: int, *,
                dpi: int = 90, quality: int = 50, mode: str = "text") -> str:
    """Emit plain text by default, or modest-resolution images on request."""
    width, height = page.rect.width, page.rect.height
    if mode == "text":
        # Keep useful table spacing, but discard heavy per-span positioning and
        # embedded imagery. Search reads this same text instead of duplicating it.
        text = "\n".join(line.rstrip() for line in page.get_text(sort=True).splitlines()).strip()
        if not text:
            text = "No extractable text on this page. Use --mode image or apply OCR to the source PDF."
        return (f'<article id="page-{number}" class="page-frame" data-number="{number}" '
                f'data-width="{width}" data-height="{height}"><div class="page-label">Page {number}</div>'
                f'<div class="page-inner"><pre class="report-text">{escape(text)}</pre></div></article>')
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    image = pixmap.tobytes("jpeg", jpg_quality=quality)
    uri = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
    parts = [f'<article id="page-{number}" class="page-frame" data-number="{number}" '
             f'data-width="{width}" data-height="{height}">',
             f'<div class="page-label">Page {number}</div>',
             f'<div class="page-inner" style="width:{width}px;height:{height}px">',
             f'<img class="paper" src="{uri}" alt="PDF page {number}" loading="lazy" decoding="async">',
             '<div class="text-layer">']
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                rect = pymupdf.Rect(span["bbox"]) * page.rotation_matrix
                parts.append(f'<span data-width="{rect.width:.3f}" style="left:{rect.x0:.3f}px;'
                             f'top:{rect.y0:.3f}px;font-size:{span["size"]:.3f}px">{escape(span["text"])}</span>')
    parts.append('</div>')
    for link in page.get_links():
        label = ""
        attributes = ""
        if link.get("kind") == pymupdf.LINK_GOTO and first <= link.get("page", -1) + 1 <= last:
            target = link["page"] + 1
            href, label = f"#page-{target}", f"Go to page {target}"
        elif link.get("kind") == pymupdf.LINK_URI and urlsplit(link.get("uri", "")).scheme.lower() in {"http", "https", "mailto"}:
            href = link["uri"]
            label = "Open external link"
            attributes = ' target="_blank" rel="noopener noreferrer"'
        if label:
            rect = pymupdf.Rect(link["from"]) * page.rotation_matrix
            parts.append(f'<a class="pdf-link" href="{escape(href, quote=True)}" aria-label="{label}" '
                         f'style="{_position(rect)}"{attributes}></a>')
    parts.append('</div></article>')
    return "\n".join(parts)


def convert_pdf_to_html(pdf_path: str | Path, output: str | Path | None = None, *,
                        start_page: int = 1, end_page: int | None = None,
                        force: bool = False, dpi: int = 90, quality: int = 50,
                        mode: str = "text") -> Path:
    """Create a standalone HTML, keeping the source untouched and output atomic."""
    source = Path(pdf_path).resolve()
    if mode not in {"text", "image"}:
        raise ValueError("mode must be text or image")
    if not 36 <= dpi <= 300:
        raise ValueError("dpi must be between 36 and 300")
    if not 1 <= quality <= 95:
        raise ValueError("quality must be between 1 and 95")
    destination = Path(output).resolve() if output is not None else source.with_suffix(".html")
    if source == destination:
        raise ValueError("HTML output must not overwrite the source PDF")
    if destination.exists() and not force:
        raise FileExistsError(f"Output exists; use --force to replace it: {destination}")
    temporary = None
    try:
        with pymupdf.open(source) as document:
            if document.needs_pass:
                raise ValueError("Password-protected PDF: decrypt it before conversion")
            total = len(document)
            last = total if end_page is None else end_page
            if not 1 <= start_page <= last <= total:
                raise ValueError(f"Page range must satisfy 1 <= start <= end <= {total}")
            title = escape(source.stem)
            bookmarks = []
            for level, label, page_number in document.get_toc():
                if start_page <= page_number <= last:
                    bookmarks.append(f'<a href="#page-{page_number}" data-page="{page_number}" '
                                     f'style="margin-left:{min(max(level - 1, 0), 6) * 12}px">{escape(label)}</a>')
            if not bookmarks:
                bookmarks = [f'<a href="#page-{n}" data-page="{n}">Page {n}</a>' for n in range(start_page, last + 1)]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                             prefix=f".{destination.name}.", suffix=".tmp", delete=False) as writer:
                temporary = Path(writer.name)
                writer.write(f'<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
                             f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                             f'<title>{title}</title><style>{CSS}</style></head><body class="{mode}-mode">')
                writer.write(f'<header class="toolbar"><button id="toggle-nav" aria-label="Toggle navigation">☰</button>'
                             f'<h1 title="{title}">{title}</h1><button id="previous" aria-label="Previous page">‹</button>'
                             f'<input id="page-number" class="page-input" aria-label="Page number" type="number" min="{start_page}" max="{last}" value="{start_page}">'
                             f'<span class="total">/ {total}</span><button id="next" aria-label="Next page">›</button>'
                             '<select id="zoom" aria-label="Zoom"><option value="fit">Fit width</option>'
                             '<option value="0.5">50%</option><option value="0.75">75%</option><option value="1">100%</option>'
                             '<option value="1.25">125%</option><option value="1.5">150%</option><option value="2">200%</option></select>'
                             '<form id="search-form"><input id="search" class="search" aria-label="Search report" placeholder="Search / 搜索">'
                             '<button type="submit">Find</button></form><span id="search-status" class="search-status" role="status"></span>'
                             '<button id="print">Print</button></header>')
                writer.write('<nav class="sidebar" aria-label="Report contents"><h2>REPORT CONTENTS / 目录</h2>' + ''.join(bookmarks) + '</nav><main>')
                pages = []
                for n in range(start_page, last + 1):
                    page = document[n - 1]
                    writer.write(render_page(page, n, start_page, last, dpi=dpi, quality=quality, mode=mode))
                    info = {"number": n, "width": page.rect.width}
                    if mode == "image":
                        info["text"] = page.get_text(sort=True)
                    pages.append(info)
                data = json.dumps({"mode": mode, "first": start_page, "last": last, "pages": pages}, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
                writer.write(f'</main><script id="report-data" type="application/json">{data}</script><script>{JS}</script></body></html>')
                writer.flush()
                os.fsync(writer.fileno())
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--output", "-o", type=Path, help="Output HTML (default: beside PDF)")
    parser.add_argument("--start-page", type=int, default=1, help="First page, one-based")
    parser.add_argument("--end-page", type=int, help="Last page, inclusive")
    parser.add_argument("--force", action="store_true", help="Replace existing HTML")
    parser.add_argument("--mode", choices=["text", "image"], default="text",
                        help="Small text-only preview (default) or larger page images")
    parser.add_argument("--dpi", type=int, default=90, help="Page image resolution, 36–300 (default: 90)")
    parser.add_argument("--quality", type=int, default=50, help="JPEG quality, 1–95 (default: 50)")
    args = parser.parse_args(argv)
    try:
        result = convert_pdf_to_html(args.pdf_path, args.output, start_page=args.start_page,
                                     end_page=args.end_page, force=args.force, dpi=args.dpi, quality=args.quality, mode=args.mode)
    except (OSError, ValueError, pymupdf.FileDataError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(result)


if __name__ == "__main__":
    main()
