import React, { useEffect, useState } from "react";
import {
  getDocumentByUrl,
  saveWebPage,
  savePdf,
  type DocumentByUrl,
  type Highlight,
} from "@/lib/api";
import KBPicker from "./KBPicker";
import StatusFeedback, { type Status } from "./StatusFeedback";

const TRACKING_PARAMS = new Set([
  "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
  "utm_id", "utm_name", "utm_brand", "utm_social",
  "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
  "_branch_match_id", "igshid",
]);

function canonicalize(href: string): string {
  try {
    const u = new URL(href);
    u.hash = "";
    const keep = new URLSearchParams();
    u.searchParams.forEach((v, k) => {
      if (!TRACKING_PARAMS.has(k.toLowerCase())) keep.append(k, v);
    });
    u.search = keep.toString() ? `?${keep.toString()}` : "";
    if (u.pathname.length > 1 && u.pathname.endsWith("/")) {
      u.pathname = u.pathname.replace(/\/+$/, "");
    }
    return u.toString();
  } catch {
    return href;
  }
}

interface Props {
  apiUrl: string;
  accessToken: string | null;
}

interface TabInfo {
  url: string;
  title: string;
  isPdf: boolean;
  tabId: number;
}

export default function SaveForm({ apiUrl, accessToken }: Props) {
  const [tab, setTab] = useState<TabInfo | null>(null);
  const [title, setTitle] = useState("");
  const [knowledgeBaseId, setKnowledgeBaseId] = useState<string | null>(null);
  const [existingDoc, setExistingDoc] = useState<DocumentByUrl | null>(null);
  const [checkingExisting, setCheckingExisting] = useState(false);
  const [status, setStatus] = useState<Status>({ type: "idle" });

  useEffect(() => {
    detectCurrentPage();
  }, []);

  useEffect(() => {
    if (!tab || tab.isPdf) {
      setExistingDoc(null);
      setCheckingExisting(false);
      return;
    }

    let cancelled = false;

    async function checkExistingDocument() {
      if (!tab) return;
      setCheckingExisting(true);
      setExistingDoc(null);
      try {
        const doc = await getDocumentByUrl(apiUrl, accessToken, canonicalize(tab.url));
        if (cancelled) return;
        if (doc) {
          setExistingDoc(doc);
          setKnowledgeBaseId(doc.knowledge_base_id);
          chrome.tabs.sendMessage(tab.tabId, {
            type: "DOCUMENT_SAVED",
            documentId: doc.id,
          }).catch(() => {
            // Content script may not be present on restricted pages.
          });
        }
      } catch {
        // A miss is normal for new pages. Other failures should not block saving.
      } finally {
        if (!cancelled) setCheckingExisting(false);
      }
    }

    checkExistingDocument();

    return () => {
      cancelled = true;
    };
  }, [apiUrl, accessToken, tab]);

  async function detectCurrentPage() {
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!activeTab?.url || !activeTab.id) return;

    const url = activeTab.url;
    const isPdf =
      url.toLowerCase().endsWith(".pdf") ||
      (activeTab.title?.toLowerCase().endsWith(".pdf") ?? false);

    setTab({ url, title: activeTab.title ?? "", isPdf, tabId: activeTab.id });
    setTitle(activeTab.title ?? "");
  }

  async function handleSave() {
    if (!tab || !knowledgeBaseId) return;

    try {
      if (tab.isPdf) {
        await handleSavePdf();
      } else {
        await handleSaveWeb();
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Save failed";
      setStatus({ type: "error", message });
    }
  }

  async function handleSaveWeb() {
    if (!tab || !knowledgeBaseId) return;

    setStatus({ type: "saving", message: "Extracting page..." });

    let html: string;
    try {
      // Run in the page so the extension's own marks/UI are stripped from
      // the snapshot — we don't want yellow <mark> nodes or the popover
      // floating in the saved HTML.
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.tabId },
        func: () => {
          const clone = document.documentElement.cloneNode(true) as HTMLElement;
          clone.querySelectorAll(
            ".llmwiki-pill, .llmwiki-popover, #llmwiki-highlight-style",
          ).forEach((el) => el.remove());
          clone.querySelectorAll("mark.llmwiki-hl").forEach((mark) => {
            const parent = mark.parentNode;
            if (!parent) return;
            while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
            parent.removeChild(mark);
          });
          return clone.outerHTML;
        },
      });
      html = result as string;
    } catch {
      throw new Error("Could not extract page content. Try refreshing the page.");
    }

    let highlights: Highlight[] = [];
    try {
      const reply = await chrome.tabs.sendMessage(tab.tabId, {
        type: "GET_PAGE_HIGHLIGHTS",
      });
      if (reply?.highlights && Array.isArray(reply.highlights)) {
        highlights = reply.highlights as Highlight[];
      }
    } catch {
      // Content script may not be present (e.g. PDF, restricted page). Ignore.
    }

    setStatus({ type: "saving", message: "Saving to LLM Wiki..." });

    const canonicalUrl = canonicalize(tab.url);

    const result = await saveWebPage(apiUrl, accessToken, knowledgeBaseId, {
      url: canonicalUrl,
      title: title || tab.title,
      html,
      highlights: highlights.length ? highlights : undefined,
    });

    // Tell the content script about the new doc id so subsequent highlight
    // edits in this same tab can persist via PATCH /highlights without a reload.
    try {
      await chrome.tabs.sendMessage(tab.tabId, {
        type: "DOCUMENT_SAVED",
        documentId: result.id,
      });
    } catch {
      // Page might be closed or content script unavailable — fine.
    }

    setExistingDoc({
      id: result.id,
      knowledge_base_id: knowledgeBaseId,
      title: title || tab.title,
      path: "/webclipper/",
      filename: "",
      version: 1,
      highlights,
    });
    setStatus({ type: "success" });
  }

  async function handleSavePdf() {
    if (!tab || !knowledgeBaseId) return;

    setStatus({ type: "saving", message: "Downloading PDF..." });

    const downloadResult = await chrome.runtime.sendMessage({
      type: "DOWNLOAD_PDF",
      url: tab.url,
    });

    if ("error" in downloadResult) {
      throw new Error(downloadResult.error);
    }

    setStatus({ type: "saving", message: "Uploading to LLM Wiki..." });

    const pdfBytes = new Uint8Array(downloadResult.blob);
    await savePdf(apiUrl, accessToken, pdfBytes, downloadResult.filename, knowledgeBaseId);

    setStatus({ type: "success" });
  }

  if (!tab) {
    return (
      <div className="flex items-center justify-center py-6">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-200 border-t-zinc-800" />
      </div>
    );
  }

  const isSaving = status.type === "saving";
  const isAlreadySaved = !!existingDoc;
  const canSave = knowledgeBaseId && !isSaving && !isAlreadySaved && status.type !== "success";

  return (
    <div className="space-y-3">
      {/* Type badge + URL */}
      <div className="flex min-w-0 items-center gap-2 rounded-md border border-zinc-200 bg-white px-2.5 py-2">
        <span
          className={`inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${
            tab.isPdf ? "bg-red-50 text-red-700" : "bg-zinc-100 text-zinc-700"
          }`}
        >
          {tab.isPdf ? "PDF" : "Web"}
        </span>
        <span className="min-w-0 truncate text-xs text-zinc-500">{tab.url}</span>
      </div>

      {/* Title */}
      <div>
        <label className="mb-1.5 block text-xs font-medium text-zinc-700">Title</label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="h-9 w-full rounded-md border border-zinc-200 bg-white px-3 text-sm
                     text-zinc-950 shadow-sm outline-none transition-colors
                     placeholder:text-zinc-400 focus:border-zinc-400 focus:ring-2
                     focus:ring-zinc-950/10"
          placeholder="Page title"
        />
      </div>

      {/* KB picker */}
      <KBPicker
        apiUrl={apiUrl}
        accessToken={accessToken}
        value={knowledgeBaseId}
        onChange={setKnowledgeBaseId}
      />

      {/* Save button */}
      <button
        onClick={handleSave}
        disabled={!canSave}
        className={`h-9 w-full rounded-md px-4 text-sm font-medium
                   transition-colors focus-visible:outline-none focus-visible:ring-2
                   focus-visible:ring-zinc-950 focus-visible:ring-offset-2
                   disabled:cursor-not-allowed ${
                     isAlreadySaved
                       ? "border border-emerald-200 bg-emerald-50 text-emerald-700"
                       : "bg-zinc-950 text-zinc-50 shadow-sm hover:bg-zinc-800 disabled:opacity-50"
                   }`}
      >
        {isSaving ? "Saving..." : isAlreadySaved ? "Already saved" : "Save to LLM Wiki"}
      </button>

      {checkingExisting && (
        <p className="text-xs text-zinc-500">Checking saved status...</p>
      )}
      {isAlreadySaved && (
        <p className="text-xs text-emerald-700">
          This page is already in LLM Wiki.
        </p>
      )}

      <StatusFeedback status={status} />
    </div>
  );
}
