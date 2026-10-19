import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { AuthGate } from "./AuthGate";
import { Cover } from "./Cover";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/shell.css";
import "./styles/feed.css";
import "./styles/team.css";
import "./styles/assistant.css";
import "./styles/settings.css";
import "./styles/cover.css";

function Root() {
  const [entered, setEntered] = useState(false);
  return (
    <AuthGate>
      {entered ? <App /> : <Cover onEnter={() => setEntered(true)} />}
    </AuthGate>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
