'use client'

import * as React from 'react'
import { useEditor, EditorContent } from '@tiptap/react'
import { Loader2 } from 'lucide-react'

import { apiFetch } from '@/lib/api'
import { useUserStore } from '@/stores'
import { cn } from '@/lib/utils'
import { createMarkdownExtensions } from '@/lib/tiptap/extensions'
import { canonicalPlaintextFromTipTapDoc } from '@/lib/highlights/canonicalPlaintext'
import { decorationsFromHighlights } from '@/lib/highlights/applyHighlights'
import { highlightPluginKey } from '@/lib/highlights/decorationPlugin'
import { sanitizeUrl } from '@/components/editor/PropertyEditors'
import type { Highlight, HighlightsResponse } from '@/lib/highlights/types'
import type { Document } from '@/lib/types'

interface ContentResponse {
  id: string
  content: string
  version: number
}

interface UrlResponse {
  url: string
}

interface WebclipAssetMetadata {
  src?: string
  path?: string
  filename?: string
  document_id?: string
}

interface Props {
  documentId: string
  className?: string
}

export default function MarkdownClipViewer({ documentId, className }: Props) {
  const token = useUserStore((s) => s.accessToken)
  const [markdown, setMarkdown] = React.useState<string | null>(null)
  const [highlights, setHighlights] = React.useState<Highlight[] | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const imageUrlsRef = React.useRef<Record<string, string>>({})

  const editor = useEditor({
    immediatelyRender: false,
    editable: false,
    extensions: createMarkdownExtensions({
      imageSrcResolver: (src) => imageUrlsRef.current[normalizeImageSrc(src)] ?? src,
    }),
    editorProps: {
      attributes: {
        class:
          'prose prose-sm dark:prose-invert max-w-none focus:outline-none select-text',
      },
      // Read mode: links don't open by default (Link extension is configured
      // with openOnClick: false in the shared factory). Keep that off so
      // selection inside a link doesn't navigate, but still let an explicit
      // click open the URL safely in a new tab.
      handleClick: (_view, _pos, event) => {
        const anchor = (event.target as HTMLElement).closest('a')
        if (!anchor) return false
        const href = anchor.getAttribute('href')
        if (!href) return false
        const safeHref = sanitizeUrl(href)
        if (safeHref) window.open(safeHref, '_blank', 'noopener,noreferrer')
        return true
      },
    },
  })

  React.useEffect(() => {
    if (!token) return
    let cancelled = false
    setError(null)
    setMarkdown(null)
    setHighlights(null)
    imageUrlsRef.current = {}

    Promise.all([
      apiFetch<Document>(`/v1/documents/${documentId}`, token),
      apiFetch<ContentResponse>(`/v1/documents/${documentId}/content`, token),
      apiFetch<HighlightsResponse>(`/v1/documents/${documentId}/highlights`, token).catch(
        () => ({ id: documentId, version: 0, highlights: [] }),
      ),
    ])
      .then(async ([doc, content, highlightResponse]) => {
        const imageUrls = await resolveWebclipAssetUrls(doc, token)
        if (cancelled) return
        imageUrlsRef.current = imageUrls
        setMarkdown(content.content ?? '')
        setHighlights(highlightResponse.highlights ?? [])
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? 'Failed to load document')
      })

    return () => {
      cancelled = true
    }
  }, [documentId, token])

  // Set content on the editor once markdown is loaded.
  React.useEffect(() => {
    if (!editor || markdown === null) return
    editor.commands.setContent(markdown, { emitUpdate: false })
  }, [editor, markdown])

  // Apply highlights once both editor + highlights are ready. Done in a
  // requestAnimationFrame to give the editor a chance to settle after
  // setContent (TipTap rebuilds the doc tree synchronously, but waiting
  // one frame avoids occasional stale-doc issues with very large docs).
  React.useEffect(() => {
    if (!editor || markdown === null || highlights === null) return
    let raf = 0
    raf = requestAnimationFrame(() => {
      const canonical = canonicalPlaintextFromTipTapDoc(editor.state.doc)
      const ranges = decorationsFromHighlights(highlights, canonical)
      editor.view.dispatch(
        editor.state.tr.setMeta(highlightPluginKey, { setDecorations: ranges }),
      )
    })
    return () => cancelAnimationFrame(raf)
  }, [editor, markdown, highlights])

  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-sm text-destructive">{error}</p>
      </div>
    )
  }

  if (markdown === null || !editor) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className={cn('h-full overflow-y-auto bg-background', className)}>
      <div className="max-w-3xl mx-auto px-8 py-10">
        <EditorContent editor={editor} />
      </div>
    </div>
  )
}

function normalizeImageSrc(src: string): string {
  return src.trim().replace(/^\.?\//, '')
}

async function resolveWebclipAssetUrls(doc: Document, token: string): Promise<Record<string, string>> {
  const metadata = doc.metadata ?? {}
  const assets = Array.isArray(metadata.assets)
    ? (metadata.assets as WebclipAssetMetadata[])
    : []
  if (!assets.length) return {}

  const pairs = await Promise.all(
    assets.map(async (asset) => {
      if (!asset.document_id) return null
      try {
        const res = await apiFetch<UrlResponse>(`/v1/documents/${asset.document_id}/url`, token)
        const keys = [asset.src, asset.path, asset.filename].filter(Boolean) as string[]
        return { keys, url: res.url }
      } catch {
        return null
      }
    }),
  )

  const urls: Record<string, string> = {}
  for (const pair of pairs) {
    if (!pair) continue
    for (const key of pair.keys) {
      urls[normalizeImageSrc(key)] = pair.url
    }
  }
  return urls
}
