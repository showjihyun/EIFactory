import { useId, useState, type FormEvent } from "react";

interface Props {
  onLogin: (username: string, password: string) => Promise<void>;
}

export function LoginForm({ onLogin }: Props) {
  const id = useId();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [failed, setFailed] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFailed(false);
    try {
      await onLogin(username, password);
    } catch {
      setFailed(true);
    }
  }

  return (
    <form className="login-form" onSubmit={handleSubmit}>
      <h2>로그인</h2>
      <div className="field">
        <label htmlFor={`${id}-username`}>아이디</label>
        <input
          id={`${id}-username`}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
        />
      </div>
      <div className="field">
        <label htmlFor={`${id}-password`}>비밀번호</label>
        <input
          id={`${id}-password`}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
      </div>
      {failed && <p className="error">로그인에 실패했습니다</p>}
      <button type="submit">로그인</button>
    </form>
  );
}
