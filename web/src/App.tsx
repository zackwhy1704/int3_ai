// Placeholder until gate 4: proves the web container builds and reaches the API.
import { useEffect, useState } from "react";

type User = { id: string; name: string; title: string };

export default function App() {
  const [users, setUsers] = useState<User[]>([]);
  useEffect(() => {
    fetch("/api/users").then((r) => r.json()).then(setUsers);
  }, []);
  return (
    <main style={{ fontFamily: "system-ui", padding: 40 }}>
      <h1>Company Brain</h1>
      <p>UI arrives at gate 4. Seeded users:</p>
      <ul>{users.map((u) => <li key={u.id}>{u.name} — {u.title}</li>)}</ul>
    </main>
  );
}
