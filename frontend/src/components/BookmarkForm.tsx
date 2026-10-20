import { useId, useState, type FormEvent } from "react";
import type { BookmarkInput } from "../types";

interface Props {
  initial?: BookmarkInput;
  submitLabel?: string;
  onSubmit: (input: BookmarkInput) => Promise<void> | void;
  onCancel?: () => void;
}

interface Errors {
  url?: string;
  title?: string;
}

export function parseTags(raw: string): string[] {
  const tags: string[] = [];
  for (const part of raw.split(",")) {
    const tag = part.trim().toLowerCase();
    if (tag && !tags.includes(tag)) tags.push(tag);
  }
  return tags;
}

function validate(url: string, title: string): Errors {
  const errors: Errors = {};
  if (!/^https?:\/\/.+/.test(url.trim())) {
    errors.url = "URL은 http:// 또는 https://로 시작해야 합니다";
  }
  if (!title.trim()) {
    errors.title = "제목을 입력하세요";
  }
  return errors;
}

export function BookmarkForm({ initial, submitLabel = "추가", onSubmit, onCancel }: Props) {
  const id = useId();
  const [url, setUrl] = useState(initial?.url ?? "");
  const [title, setTitle] = useState(initial?.title ?? "");
  const [tags, setTags] = useState(initial?.tags.join(", ") ?? "");
  const [errors, setErrors] = useState<Errors>({});

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const found = validate(url, title);
    setErrors(found);
    if (found.url || found.title) return;
    await onSubmit({ url: url.trim(), title: title.trim(), tags: parseTags(tags) });
    if (!initial) {
      setUrl("");
      setTitle("");
      setTags("");
    }
  }

  return (
    <form className="bookmark-form" onSubmit={handleSubmit} noValidate>
      <div className="field">
        <label htmlFor={`${id}-url`}>URL</label>
        <input
          id={`${id}-url`}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://"
        />
        {errors.url && <p className="error">{errors.url}</p>}
      </div>
      <div className="field">
        <label htmlFor={`${id}-title`}>제목</label>
        <input id={`${id}-title`} value={title} onChange={(e) => setTitle(e.target.value)} />
        {errors.title && <p className="error">{errors.title}</p>}
      </div>
      <div className="field">
        <label htmlFor={`${id}-tags`}>태그 (쉼표로 구분)</label>
        <input id={`${id}-tags`} value={tags} onChange={(e) => setTags(e.target.value)} />
      </div>
      <div className="actions">
        <button type="submit">{submitLabel}</button>
        {onCancel && (
          <button type="button" onClick={onCancel}>
            취소
          </button>
        )}
      </div>
    </form>
  );
}
