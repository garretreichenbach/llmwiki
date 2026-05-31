import React, { useEffect, useState } from "react";
import { getMode, setMode, getLocalUrl, setLocalUrl, type Mode } from "@/lib/settings";

interface Props {
  onBack: () => void;
  onModeChange: (mode: Mode) => void;
}

export default function Settings({ onBack, onModeChange }: Props) {
  const [mode, setModeState] = useState<Mode>("cloud");
  const [localUrl, setLocalUrlState] = useState("http://localhost:8000");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getMode().then(setModeState);
    getLocalUrl().then(setLocalUrlState);
  }, []);

  async function handleModeChange(newMode: Mode) {
    setModeState(newMode);
    await setMode(newMode);
    onModeChange(newMode);
    flash();
  }

  async function handleUrlSave() {
    await setLocalUrl(localUrl);
    flash();
  }

  function flash() {
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <div className="space-y-4">
      <button
        onClick={onBack}
        className="rounded-md px-2 py-1 text-xs font-medium text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900"
      >
        &larr; Back
      </button>

      <div>
        <label className="mb-2 block text-xs font-medium text-zinc-700">Mode</label>
        <div className="grid grid-cols-2 gap-1 rounded-md border border-zinc-200 bg-zinc-100 p-1">
          <button
            onClick={() => handleModeChange("cloud")}
            className={`h-8 rounded-sm px-3 text-sm font-medium transition-colors ${
              mode === "cloud"
                ? "bg-white text-zinc-950 shadow-sm"
                : "text-zinc-500 hover:text-zinc-900"
            }`}
          >
            Cloud
          </button>
          <button
            onClick={() => handleModeChange("local")}
            className={`h-8 rounded-sm px-3 text-sm font-medium transition-colors ${
              mode === "local"
                ? "bg-white text-zinc-950 shadow-sm"
                : "text-zinc-500 hover:text-zinc-900"
            }`}
          >
            Local
          </button>
        </div>
        <p className="mt-1.5 text-[11px] leading-4 text-zinc-500">
          {mode === "cloud"
            ? "Saves to llmwiki.app, requires sign in"
            : "Saves to your local LLM Wiki instance, no sign in needed"}
        </p>
      </div>

      {mode === "local" && (
        <div>
          <label className="mb-1.5 block text-xs font-medium text-zinc-700">
            API URL
          </label>
          <div className="flex gap-2">
            <input
              value={localUrl}
              onChange={(e) => setLocalUrlState(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleUrlSave(); }}
              className="h-9 min-w-0 flex-1 rounded-md border border-zinc-200 bg-white px-3
                         font-mono text-xs text-zinc-950 shadow-sm outline-none
                         transition-colors focus:border-zinc-400 focus:ring-2
                         focus:ring-zinc-950/10"
              placeholder="http://localhost:8000"
            />
            <button
              onClick={handleUrlSave}
              className="h-9 rounded-md bg-zinc-950 px-3 text-xs font-medium text-zinc-50
                         transition-colors hover:bg-zinc-800"
            >
              Save
            </button>
          </div>
        </div>
      )}

      {saved && (
        <p className="text-xs text-emerald-700">Settings saved</p>
      )}
    </div>
  );
}
