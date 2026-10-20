import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import { BookmarkForm } from "./components/BookmarkForm";
import { BookmarkList } from "./components/BookmarkList";
import { LoginForm } from "./components/LoginForm";
import type { Bookmark, BookmarkInput } from "./types";

type LoadState = "loading" | "ready" | "error";

export default function App() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [state, setState] = useState<LoadState>("loading");

  const load = useCallback(async () => {
    setState("loading");
    try {
      const [found, allTags] = await Promise.all([api.listBookmarks({ q, tag }), api.listTags()]);
      setBookmarks(found);
      setTags(allTags);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [q, tag]);

  useEffect(() => {
    if (!loggedIn) return;
    // 로그인 상태와 필터가 바뀔 때마다 서버에서 목록을 다시 불러온다.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [loggedIn, load]);

  async function handleLogin(username: string, password: string) {
    await api.login(username, password);
    setLoggedIn(true);
  }

  async function handleLogout() {
    await api.logout();
    setLoggedIn(false);
    setBookmarks([]);
  }

  async function handleCreate(input: BookmarkInput) {
    await api.createBookmark(input);
    await load();
  }

  async function handleUpdate(id: number, input: BookmarkInput) {
    await api.updateBookmark(id, input);
    await load();
  }

  async function handleDelete(id: number) {
    await api.deleteBookmark(id);
    await load();
  }

  return (
    <div className="app">
      <header className="header">
        <h1>북마크</h1>
        {loggedIn && (
          <button type="button" onClick={handleLogout}>
            로그아웃
          </button>
        )}
      </header>
      <main>
        {!loggedIn ? (
          <LoginForm onLogin={handleLogin} />
        ) : (
          <>
            <BookmarkForm onSubmit={handleCreate} />
            <div className="filters">
              <label>
                검색
                <input type="search" value={q} onChange={(e) => setQ(e.target.value)} />
              </label>
              <label>
                태그 필터
                <select value={tag} onChange={(e) => setTag(e.target.value)}>
                  <option value="">전체</option>
                  {tags.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {state === "loading" && <p className="loading">불러오는 중…</p>}
            {state === "error" && (
              <div className="error-state">
                <p>북마크를 불러오지 못했습니다</p>
                <button type="button" onClick={() => void load()}>
                  다시 시도
                </button>
              </div>
            )}
            {state === "ready" && (
              <BookmarkList bookmarks={bookmarks} onDelete={handleDelete} onUpdate={handleUpdate} />
            )}
          </>
        )}
      </main>
    </div>
  );
}
