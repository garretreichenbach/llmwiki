import React, { useEffect, useRef, useState } from "react";
import AuthGate from "./components/AuthGate";
import SaveForm from "./components/SaveForm";
import Settings from "./components/Settings";
import { getMode, getApiUrl, type Mode } from "@/lib/settings";

type View = "main" | "settings";

type AuthState =
  | { status: "loading" }
  | { status: "signed_out" }
  | { status: "signed_in"; accessToken: string }
  | { status: "local" };

export default function App() {
  const [view, setView] = useState<View>("main");
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });
  const [authError, setAuthError] = useState<string | null>(null);
  const [authNotice, setAuthNotice] = useState<string | null>(null);
  const [apiUrl, setApiUrl] = useState("");
  const [mode, setModeState] = useState<Mode>("cloud");
  const authNoticeTimer = useRef<number | null>(null);

  useEffect(() => {
    init();
  }, []);

  useEffect(() => {
    return () => {
      if (authNoticeTimer.current) window.clearTimeout(authNoticeTimer.current);
    };
  }, []);

  function showAuthNotice(message: string) {
    setAuthNotice(message);
    if (authNoticeTimer.current) window.clearTimeout(authNoticeTimer.current);
    authNoticeTimer.current = window.setTimeout(() => {
      setAuthNotice(null);
      authNoticeTimer.current = null;
    }, 3500);
  }

  async function init() {
    const currentMode = await getMode();
    const url = await getApiUrl();
    setModeState(currentMode);
    setApiUrl(url);

    if (currentMode === "local") {
      setAuth({ status: "local" });
    } else {
      await checkSession();
    }
  }

  async function checkSession() {
    const { accessToken } = await chrome.runtime.sendMessage({
      type: "GET_SESSION",
    });
    if (accessToken) {
      setAuth({ status: "signed_in", accessToken });
    } else {
      setAuth({ status: "signed_out" });
    }
  }

  async function handleSignIn() {
    setAuthError(null);
    setAuth({ status: "loading" });
    const result = await chrome.runtime.sendMessage({
      type: "SIGN_IN_WITH_GOOGLE",
    });
    if (result.success) {
      await checkSession();
      showAuthNotice("Signed in to LLM Wiki");
    } else {
      setAuthError(result.error ?? "Sign in failed");
      setAuth({ status: "signed_out" });
    }
  }

  async function handleSignOut() {
    setAuthError(null);
    setAuthNotice(null);
    await chrome.runtime.sendMessage({ type: "SIGN_OUT" });
    setAuth({ status: "signed_out" });
  }

  async function handleModeChange(newMode: Mode) {
    setModeState(newMode);
    const url = await getApiUrl();
    setApiUrl(url);

    if (newMode === "local") {
      setAuthError(null);
      setAuthNotice(null);
      setAuth({ status: "local" });
    } else {
      setAuth({ status: "loading" });
      await checkSession();
    }
  }

  if (view === "settings") {
    return (
      <div className="w-full overflow-hidden rounded-lg border border-zinc-200 bg-zinc-50 p-4 font-sans text-zinc-950 shadow-[0_8px_30px_rgba(15,23,42,0.14),0_1px_2px_rgba(15,23,42,0.08)] ring-1 ring-white/80">
        <Settings onBack={() => setView("main")} onModeChange={handleModeChange} />
      </div>
    );
  }

  const isReady = auth.status === "signed_in" || auth.status === "local";
  const accessToken = auth.status === "signed_in" ? auth.accessToken : null;

  return (
    <div className="w-full overflow-hidden rounded-lg border border-zinc-200 bg-zinc-50 p-4 font-sans text-zinc-950 shadow-[0_8px_30px_rgba(15,23,42,0.14),0_1px_2px_rgba(15,23,42,0.08)] ring-1 ring-white/80">
      {/* Header */}
      <div className="mb-4 flex items-center justify-between">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-sm font-semibold tracking-normal text-zinc-950">LLM Wiki</h1>
          {mode === "local" && (
            <span className="rounded border border-zinc-200 bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-600">
              local
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {auth.status === "signed_in" && (
            <button
              onClick={handleSignOut}
              className="rounded-md px-2 py-1 text-xs font-medium text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900"
            >
              Sign out
            </button>
          )}
          <button
            onClick={() => setView("settings")}
            className="rounded-md px-2 py-1 text-xs font-medium text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900"
          >
            Settings
          </button>
        </div>
      </div>

      {/* Body */}
      {auth.status === "loading" && (
        <div className="flex items-center justify-center py-8">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-200 border-t-zinc-800" />
        </div>
      )}

      {auth.status === "signed_out" && (
        <>
          {authError && (
            <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {authError}
            </div>
          )}
          <AuthGate onSignIn={handleSignIn} />
        </>
      )}

      {authNotice && auth.status === "signed_in" && (
        <div className="mb-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          {authNotice}
        </div>
      )}

      {isReady && apiUrl && (
        <SaveForm apiUrl={apiUrl} accessToken={accessToken} />
      )}
    </div>
  );
}
