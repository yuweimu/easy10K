import { useEffect, useRef, useState } from "react";

export type SourceSelection = {
  stockCode: string; date: string; itemId: number; label: string; creationTime: string;
};
type SourceLocation = {
  pdf_hash: string; page_number: number; bbox: number[];
  excerpt_top: number; excerpt_bottom: number; image_url: string; page_image_url: string;
  amount_unit: string; statement_amount: string;
  download_url: string | null;
};

export function SourcePreview({ selection, apiBase, language, onClose }: {
  selection: SourceSelection; apiBase: string; language: "en" | "zh"; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [source, setSource] = useState<SourceLocation | null>(null);
  const [error, setError] = useState("");
  const [fallbackHash, setFallbackHash] = useState("");
  const [fallbackDownloadUrl, setFallbackDownloadUrl] = useState("");
  const [fullPage, setFullPage] = useState(false);
  const [highResolution, setHighResolution] = useState(false);
  const [imageError, setImageError] = useState(false);
  const zh = language === "zh";
  useEffect(() => {
    dialog.current?.showModal();
    const controller = new AbortController();
    const query = new URLSearchParams({ report_creation_time: selection.creationTime });
    const path = `/balance_sheet_items/${encodeURIComponent(selection.stockCode)}/${encodeURIComponent(selection.date)}/${selection.itemId}/source?${query}`;
    void fetch(`${apiBase}${path}`, { signal: controller.signal }).then(async response => {
      const data = await response.json();
      if (!response.ok) {
        setFallbackHash(data.detail?.pdf_hash || "");
        setFallbackDownloadUrl(data.detail?.download_url || "");
        throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || "Source unavailable");
      }
      setSource(data);
    }).catch(requestError => {
      if (!controller.signal.aborted) setError(String(requestError.message || requestError));
    });
    return () => controller.abort();
  }, [apiBase, selection]);
  const hash = source?.pdf_hash || fallbackHash;
  let originalUrl = "";
  try {
    const url = new URL(source?.download_url || fallbackDownloadUrl);
    if (url.protocol === "https:" || url.protocol === "http:") {
      if (source) url.hash = `page=${source.page_number}`;
      originalUrl = url.href;
    }
  } catch {
    // A missing or invalid external URL still leaves the local-copy link available.
  }
  const imagePath = source && (fullPage ? source.page_image_url : source.image_url);
  const imageUrl = imagePath && `${apiBase}${imagePath}${imagePath.includes("?") ? "&" : "?"}width=${highResolution ? 1600 : 960}`;
  const top = source && !fullPage ? source.excerpt_top : 0;
  const bottom = source && !fullPage ? source.excerpt_bottom : 1;
  return <dialog ref={dialog} className="source-preview-dialog" aria-labelledby="source-preview-title"
    onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <header className="source-preview-toolbar">
      <div><h2 id="source-preview-title">{selection.label}</h2>
        <p>{selection.stockCode} · {selection.date}{source && ` · ${zh ? "PDF 第" : "PDF page "}${source.page_number}${zh ? "页" : ""}`}</p></div>
      <button type="button" onClick={onClose} aria-label={zh ? "关闭来源预览" : "Close source preview"}>×</button>
    </header>
    {!source && !error && <p role="status">{zh ? "正在定位原始报表…" : "Locating the source statement…"}</p>}
    {error && <p role="alert" className="error-message">{error}</p>}
    {source && <>
      <div className="source-preview-controls">
        <strong>{zh ? "原报表单位：" : "Source unit: "}{source.amount_unit}</strong>
        <span>{zh ? "原始金额：" : "Source amount: "}{source.statement_amount}</span>
        <button type="button" aria-pressed={fullPage} onClick={() => { setFullPage(!fullPage); setImageError(false); }}>
          {fullPage ? (zh ? "查看行附近" : "Show excerpt") : (zh ? "展开整页" : "Show full page")}</button>
        <button type="button" aria-pressed={highResolution} onClick={() => { setHighResolution(!highResolution); setImageError(false); }}>
          {highResolution ? (zh ? "标准清晰度" : "Standard resolution") : (zh ? "高清" : "Higher resolution")}</button>
      </div>
      <p className="source-preview-caption">{zh ? "黄色框标记与该金额匹配的原始行。" : "The yellow outline marks the matching source row."}</p>
      {imageError && <p role="alert" className="error-message">{zh ? "图片加载失败，请打开原始 PDF。" : "Image unavailable. Please open the original PDF."}</p>}
      <div className="source-page-image">
        <img src={imageUrl || undefined} alt={`${selection.label} · ${selection.date} · PDF ${source.page_number}`}
          decoding="async" onError={() => setImageError(true)} />
        {!imageError && <span className="source-row-highlight" aria-hidden="true" style={{
          left: `${source.bbox[0] * 100}%`, top: `${(source.bbox[1] - top) / (bottom - top) * 100}%`,
          width: `${(source.bbox[2] - source.bbox[0]) * 100}%`,
          height: `${(source.bbox[3] - source.bbox[1]) / (bottom - top) * 100}%`,
        }} />}
      </div>
    </>}
    {(originalUrl || hash) && <p className="source-pdf-links">
      {originalUrl && <a href={originalUrl} target="_blank" rel="noreferrer">{zh ? "打开原站 PDF" : "Open PDF on original site"}</a>}
      {hash && <a href={`${apiBase}/report-digestion/file?pdf_hash=${encodeURIComponent(hash)}#page=${source?.page_number || 1}`}
        target="_blank" rel="noreferrer">{zh ? "打开本地副本" : "Open local copy"}</a>}
    </p>}
  </dialog>;
}
