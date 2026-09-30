import { useEffect, useState, type FormEvent } from "react";
import "./styles.css";

type Session = { user: { id: string; name: string; email: string }; tenant: { id: string; name: string }; csrf_token: string };
type ProviderInfo = { id: string; label: string };
type Brain = { id: string; name: string; kind: string; description: string; owner: string };
type Brains = { open: Brain[]; locked: { name: string; owner: string } | null };
type Citation = {
  chunk_id: number; document_id: string; doc_title: string; source: string;
  owner: string; effective_date: string; scope: string; text: string;
};
type FactValue = { value: string; quote: string; valid_from: string; chunk_id: number; doc_title: string };
type Fact = {
  subject: string; attribute: string; condition: string | null; scope: string;
  current: FactValue; previous: FactValue | null;
};
type Cache = { cached: boolean; cached_at?: string };
type Answer = Cache & (
  | { refused: false; answer: string; citations: Citation[]; answered_from: string[]; facts: Fact[] }
  | { refused: true; message: string; suggested_owner: string | null });
type Doc = { id: string; title: string; source: string; owner: string; effective_date: string; body: string };

class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

// Identity comes only from the httpOnly session cookie; the browser never holds a token.
// State-changing requests also carry the per-session CSRF token.
let csrfToken = "";

function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init?.method && init.method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  return fetch(path, { ...init, credentials: "same-origin", headers }).then(async (r) => {
    if (!r.ok) {
      const detail = await r.json().then((b) => b.detail).catch(() => null);
      throw new HttpError(r.status, detail ?? `${r.status} ${r.statusText}`);
    }
    return r.json();
  });
}

const LOGIN_ERRORS: Record<string, string> = {
  not_invited: "That account hasn't been invited. Ask your administrator to add you.",
  email_not_verified: "Your Google email address isn't verified.",
  rejected: "Sign-in was refused. Try again, or ask your administrator.",
};

const day = (iso: string) =>
  new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });

export default function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [brains, setBrains] = useState<Brains | null>(null);
  const [brainId, setBrainId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [doc, setDoc] = useState<{ doc: Doc; passage: string } | null>(null);
  const loginError = new URLSearchParams(window.location.search).get("login_error");

  useEffect(() => {
    api<Session>("/api/session")
      .then((s) => { csrfToken = s.csrf_token; setSession(s); })
      .catch(() => {
        setSession(null);
        api<ProviderInfo[]>("/api/auth/providers").then(setProviders);
      });
  }, []);

  useEffect(() => {
    if (session) api<Brains>("/api/brains").then(setBrains).catch(handle);
  }, [session]);

  function handle(err: unknown) {
    if (err instanceof HttpError && err.status === 401) { setSession(null); return; }
    setError(err instanceof Error ? err.message : String(err));
  }

  function reset() {
    setBrainId(null); setQuestion("");
    setAnswer(null); setDoc(null); setError(""); setAsked("");
  }

  async function signOut() {
    await api("/api/auth/logout", { method: "POST" }).catch(() => null);
    window.location.href = "/";
  }

  const brainName = (id: string) => brains?.open.find((b) => b.id === id)?.name ?? id;
  // A conditional fact that coexists with the default value of the same thing.
  const variants = (facts: Fact[]) =>
    facts.filter((f) => f.condition && facts.some((d) =>
      !d.condition && d.subject === f.subject && d.attribute === f.attribute));

  function submit(e: FormEvent) {
    e.preventDefault();
    ask(question);
  }

  async function ask(q: string) {
    if (!q.trim()) return;
    setQuestion(q);
    setBusy(true); setAnswer(null); setDoc(null); setError(""); setAsked(q);
    try {
      setAnswer(await api<Answer>("/api/ask", {
        method: "POST", body: JSON.stringify({ question: q, brain_id: brainId }),
      }));
    } catch (err) {
      handle(err);
    } finally {
      setBusy(false);
    }
  }

  async function open(c: Citation) {
    try {
      setDoc({ doc: await api<Doc>(`/api/documents/${c.document_id}`), passage: c.text });
    } catch (err) {
      handle(err);
    }
  }

  if (session === undefined) return null;

  if (session === null) {
    return (
      <div className="login">
        <div className="card">
          <div><span className="dot" /> <strong>Company Brain</strong></div>
          <h1>Sign in</h1>
          {loginError && <div className="card refusal">{LOGIN_ERRORS[loginError] ?? LOGIN_ERRORS.rejected}</div>}
          {providers.map((p) => (
            <a key={p.id} className="provider" href={`/api/auth/login/${p.id}`}>Continue with {p.label}</a>
          ))}
          {providers.length === 0 && <p className="muted">No sign-in provider is configured.</p>}
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header>
        <span className="dot" /> <strong>Company Brain</strong>
        <span className="grow" />
        <button className="reset" onClick={reset}>Reset</button>
        <span className="small">
          {session.user.name} · {session.user.email} · <strong>{session.tenant.name}</strong>
        </span>
        <button className="reset signout" onClick={signOut}>Sign out</button>
      </header>

      <div className="body">
        <nav>
          <div className="label">ASKING</div>
          <button className={brainId === null ? "brain active" : "brain"} onClick={() => setBrainId(null)}>
            All my brains
          </button>
          {brains?.open.map((b) => (
            <button key={b.id} className={brainId === b.id ? "brain active" : "brain"}
                    onClick={() => setBrainId(b.id)} title={b.description}>
              {b.name}
            </button>
          ))}
          {brains?.locked && (
            <div className="brain locked" aria-label="No access">
              <div>🔒 {brains.locked.name}</div>
              <div className="muted small">No access · owner {brains.locked.owner}</div>
            </div>
          )}
        </nav>

        <main>
          <form onSubmit={submit} className="ask">
            <label htmlFor="q" className="label">ASK ANYTHING</label>
            <div className="row">
              <input id="q" value={question} onChange={(e) => setQuestion(e.target.value)} />
              <button type="submit" disabled={busy}>{busy ? "Asking…" : "Ask"}</button>
            </div>
          </form>


          {error && <div className="card refusal">Couldn't get an answer: {error}</div>}

          {answer && <h2>{asked}</h2>}

          {answer?.cached && (
            <div className="cached">
              Cached answer: the live model call just failed, so this is the real answer
              generated for the same question and access on {new Date(answer.cached_at!).toLocaleString("en-GB")}.
            </div>
          )}

          {answer?.refused === true && (
            <div className="card refusal">
              <div className="headline">{answer.message}</div>
              {answer.suggested_owner && <p>Try asking <strong>{answer.suggested_owner}</strong>.</p>}
            </div>
          )}

          {answer?.refused === false && (
            <div className="card">
              <div className="headline">{answer.answer}</div>

              {answer.facts.filter((f) => f.previous).map((f) => (
                <div key={`chg-${f.current.chunk_id}-${f.attribute}`} className="change">
                  <div className="change-title">This changed on {day(f.current.valid_from)}</div>
                  <div>It used to say <strong>{f.previous!.value}</strong>.</div>
                  <div className="versions">
                    <div>
                      <div className="small muted">NOW · since {day(f.current.valid_from)} · {f.current.doc_title}</div>
                      <blockquote>“{f.current.quote}”</blockquote>
                    </div>
                    <div>
                      <div className="small muted">BEFORE · from {day(f.previous!.valid_from)} · {f.previous!.doc_title}</div>
                      <blockquote>“{f.previous!.quote}”</blockquote>
                    </div>
                  </div>
                </div>
              ))}

              {variants(answer.facts).length > 0 && (
                <div className="conditions">
                  <div className="label">ALSO TRUE, UNDER A CONDITION</div>
                  {variants(answer.facts).map((f) => (
                    <div key={`cond-${f.current.chunk_id}-${f.attribute}-${f.condition}`}>
                      <strong>{f.attribute}</strong> for <em>{f.condition}</em>:{" "}
                      <strong>{f.current.value}</strong>{" "}
                      <span className="muted small">
                        (since {day(f.current.valid_from)} · {f.current.doc_title} · {brainName(f.scope)})
                      </span>
                      <blockquote>“{f.current.quote}”</blockquote>
                    </div>
                  ))}
                </div>
              )}

              <div className="label">WHERE THIS CAME FROM</div>
              {answer.citations.map((c) => (
                <div key={c.chunk_id} className="source">
                  <div className="grow">
                    <div><strong>{c.doc_title}</strong></div>
                    <div className="small muted">
                      {c.source} · {day(c.effective_date)} · owner {c.owner} · {brainName(c.scope)}
                    </div>
                  </div>
                  <button className="link" onClick={() => open(c)}>Open</button>
                </div>
              ))}

              <div className="footer">
                Answered from <strong>{answer.answered_from.map(brainName).join(", ")}</strong>.
                You can open every source yourself.
              </div>
            </div>
          )}

          {doc && (
            <div className="card doc">
              <div className="row">
                <div className="grow">
                  <strong>{doc.doc.title}</strong>
                  <div className="small muted">
                    {doc.doc.source} · {day(doc.doc.effective_date)} · owner {doc.doc.owner}
                  </div>
                </div>
                <button className="link" onClick={() => setDoc(null)}>Close</button>
              </div>
              {doc.doc.body.split("\n\n").map((p, i) => (
                <p key={i} className={p.trim() === doc.passage ? "cited" : ""}>{p}</p>
              ))}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
