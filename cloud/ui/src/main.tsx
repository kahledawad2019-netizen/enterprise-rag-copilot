import { StrictMode, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { ClerkLoaded, ClerkLoading, ClerkProvider, Show, SignIn, UserButton, useAuth } from "@clerk/react";

import App from "./App";
import { setSessionTokenProvider } from "./api";
import "./styles.css";
import "./app.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

/**
 * Sign-in is optional. The Clerk-gated gateway deployment sets
 * VITE_CLERK_PUBLISHABLE_KEY at build time; the local app and the public demo
 * do not, and talk to a backend that decides for itself whether anonymous
 * requests are allowed (API_AUTH_MODE).
 */
const publishableKey =
  import.meta.env.VITE_AUTH_PROVIDER === "none"
    ? undefined
    : (import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined);

function ClerkSession() {
  const { getToken, userId } = useAuth();
  // Installed before child effects can call the API. Tokens stay inside
  // Clerk's session machinery and are never copied to localStorage.
  useLayoutEffect(() => {
    setSessionTokenProvider(() => getToken());
    return () => setSessionTokenProvider(null);
  }, [getToken]);
  return <App key={userId} account={<UserButton />} persistHistory={false} />;
}

createRoot(container).render(
  <StrictMode>
    {publishableKey ? (
      <ClerkProvider publishableKey={publishableKey}>
        <ClerkLoading>
          <div className="auth-screen">Loading secure sign-in…</div>
        </ClerkLoading>
        <ClerkLoaded>
          <Show when="signed-out">
            <main className="auth-screen">
              <SignIn routing="hash" />
            </main>
          </Show>
          <Show when="signed-in">
            <ClerkSession />
          </Show>
        </ClerkLoaded>
      </ClerkProvider>
    ) : (
      <App persistHistory={true} />
    )}
  </StrictMode>,
);
