import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ClerkLoaded, ClerkLoading, ClerkProvider, Show, SignIn } from "@clerk/react";

import App from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined;
if (!publishableKey) {
  throw new Error("VITE_CLERK_PUBLISHABLE_KEY is not configured");
}

createRoot(container).render(
  <StrictMode>
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
          <App />
        </Show>
      </ClerkLoaded>
    </ClerkProvider>
  </StrictMode>,
);
