declare module "*?raw" {
  const content: string;
  export default content;
}

// The host's own settings script, loaded for its side effects.
declare module "*/_settings/settings.js";
