import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState, type FormEvent } from "react";

import { register } from "@/api/auth";
import { AuthLayout, Field } from "@/components/myra/auth-layout";
import { useAuth } from "@/lib/auth";
import { useHydrated } from "@/lib/use-hydrated";

export const Route = createFileRoute("/register")({
  head: () => ({
    meta: [
      { title: "Create your Myra account" },
      {
        name: "description",
        content: "Create a Myra account to start building with your AI coding agent.",
      },
      { property: "og:title", content: "Create your Myra account" },
      {
        property: "og:description",
        content: "Create a Myra account to start building with your AI coding agent.",
      },
    ],
  }),
  component: RegisterPage,
});

function RegisterPage() {
  const navigate = useNavigate();
  const { signIn, token, ready } = useAuth();
  const hydrated = useHydrated();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (ready && token) navigate({ to: "/chat" });
  }, [ready, token, navigate]);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (password.length < 6) {
      setError("Password must be at least 6 characters.");
      return;
    }
    setLoading(true);
    try {
      const auth = await register({ name, email, password });
      signIn(auth);
      navigate({ to: "/chat" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to register.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthLayout
      title="Create account"
      subtitle="Set up your Myra workspace."
      footer={
        <>
          Already registered?{" "}
          <Link to="/login" className="text-primary hover:opacity-80">
            Log in
          </Link>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-5">
        <Field id="name" label="Name" value={name} onChange={setName} autoComplete="name" />
        <Field
          id="email"
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="username"
        />
        <Field
          id="password"
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
        />
        <Field
          id="confirm"
          label="Confirm password"
          type="password"
          value={confirm}
          onChange={setConfirm}
          autoComplete="new-password"
        />

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={loading || !hydrated}
          className="w-full rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {loading ? "Creating account…" : "Register"}
        </button>
      </form>
    </AuthLayout>
  );
}
