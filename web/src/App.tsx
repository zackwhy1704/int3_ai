import { useEffect, useState, type FormEvent } from "react";
import "./styles.css";

type User = { id: string; name: string; title: string };
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
type Answer =
  | { refused: false; answer: string; citations: Citation[]; answered_from: string[]; facts: Fact[] }
  | { refused: true; message: string; suggested_owner: string | null };
type Doc = { id: string; title: string; source: string; owner: string; effective_date: string; body: string };

// Demo only: X-User-Id replaces authentication, is trivially spoofable, and never ships.
function api<T>(path: string, user: string, init?: RequestInit): Promise<T> {
  return fetch(path, {
    ...init,
    headers: { "X-User-Id": user, "Content-Type": "application/json" },
  }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });
}

const day = (iso: string) =>
  new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });

export default function App() {
  const [users, setUsers] = useState<User[]>([]);
  const [user, setUser] = useState("priya");
  const [brains, setBrains] = useState<Brains | null>(null);
  const [brainId, setBrainId] = useState<string | null>(null);
  const [question, setQuestion] = useState("What's our refund window for enterprise customers?");
  const [asked, setAsked] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [doc, setDoc] = useState<{ doc: Doc; passage: string } | null>(null);

  useEffect(() => {
    fetch("/api/users").then((r) => r.json()).then(setUsers);
  }, []);

  useEffect(() => {
    setBrains(null); setBrainId(null); setAnswer(null); setDoc(null); setError("");
    api<Brains>("/api/brains", user).then(setBrains);
  }, [user]);

  const brainName = (id: string) => brains?.open.find((b) => b.id === id)?.name ?? id;
  // A conditional fact that coexists with the default value of the same thing.
  const variants = (facts: Fact[]) =>
    facts.filter((f) => f.condition && facts.some((d) =>
      !d.condition && d.subject === f.subject && d.attribute === f.attribute));

  async function ask(e: FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;
    setBusy(true); setAnswer(null); setDoc(null); setError(""); setAsked(question);
    try {
      setAnswer(await api<Answer>("/api/ask", user, {
        method: "POST", body: JSON.stringify({ question, brain_id: brainId }),
      }));
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function open(c: Citation) {
    setDoc({ doc: await api<Doc>(`/api/documents/${c.document_id}`, user), passage: c.text });
  }

  return (
    <div className="app">
      <header>
        <span className="dot" /> <strong>Company Brain</strong>
        <span className="grow" />
        <label>
          Signed in as{" "}
          <select value={user} onChange={(e) => setUser(e.target.value)}>
            {users.map((u) => <option key={u.id} value={u.id}>{u.name} · {u.title}</option>)}
          </select>
        </label>
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
          <form onSubmit={ask} className="ask">
            <label htmlFor="q" className="label">ASK ANYTHING</label>
            <div className="row">
              <input id="q" value={question} onChange={(e) => setQuestion(e.target.value)} />
              <button type="submit" disabled={busy}>{busy ? "Asking…" : "Ask"}</button>
            </div>
          </form>

          {error && <div className="card refusal">Request failed: {error}</div>}

          {answer && <h2>{asked}</h2>}

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
