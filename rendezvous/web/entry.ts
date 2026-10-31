import "./styles.css";

// This page serves calls (#/call/...). Settings and key requests on a phone
// open /settings. A #/setup/ link comes from an older Sleeptalk on the host.
if (location.hash.startsWith("#/setup/")) {
  history.replaceState(null, "", location.pathname);
  document.querySelector("main.stage")?.remove();
  const note = document.createElement("p");
  note.className = "connect-step";
  note.textContent = "This link comes from an older Sleeptalk. Update Sleeptalk on your agent's computer, then ask your agent for a new link.";
  document.body.append(note);
} else {
  void import("./main");
}
