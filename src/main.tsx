import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { AuthGate } from "./AuthGate";
import "./styles.css";
import "./v3.css";
import "./v4.css";
import "./v5.css";
import "./v6.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthGate><App /></AuthGate>
  </React.StrictMode>,
);
