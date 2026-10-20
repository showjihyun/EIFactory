import { useState } from "react";
import type { Bookmark, BookmarkInput } from "../types";
import { BookmarkForm } from "./BookmarkForm";

interface Props {
  bookmarks: Bookmark[];
  onDelete: (id: number) => void;
  onUpdate: (id: number, input: BookmarkInput) => Promise<void> | void;
}

export function formatDate(iso: string): string {
  const date = new Date(iso);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

export function BookmarkList({ bookmarks, onDelete, onUpdate }: Props) {
  const [editingId, setEditingId] = useState<number | null>(null);

  if (bookmarks.length === 0) {
    return <p className="empty">아직 북마크가 없습니다</p>;
  }

  return (
    <ul className="bookmark-list">
      {bookmarks.map((bookmark) => (
        <li key={bookmark.id} className="bookmark-item" data-testid="bookmark-item">
          {editingId === bookmark.id ? (
            <BookmarkForm
              initial={{ url: bookmark.url, title: bookmark.title, tags: bookmark.tags }}
              submitLabel="저장"
              onSubmit={async (input) => {
                await onUpdate(bookmark.id, input);
                setEditingId(null);
              }}
              onCancel={() => setEditingId(null)}
            />
          ) : (
            <>
              <div className="bookmark-main">
                <a href={bookmark.url} target="_blank" rel="noopener noreferrer">
                  {bookmark.title}
                </a>
                <span className="url">{bookmark.url}</span>
              </div>
              <div className="bookmark-meta">
                {bookmark.tags.map((tag) => (
                  <span key={tag} className="tag">
                    {tag}
                  </span>
                ))}
                <time dateTime={bookmark.created_at}>{formatDate(bookmark.created_at)}</time>
              </div>
              <div className="actions">
                <button type="button" onClick={() => setEditingId(bookmark.id)}>
                  수정
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (window.confirm("이 북마크를 삭제할까요?")) onDelete(bookmark.id);
                  }}
                >
                  삭제
                </button>
              </div>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}
