import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it } from "vitest";

const source = readFileSync(new URL("../../sleeptalk/_settings/settings.js", import.meta.url), "utf8");
const helpers = source.slice(source.indexOf("const DISCORD_FAILURES"), source.indexOf("function discordSetupCard"));

it("shows the host's setup refusal and keeps friendly Discord error fallbacks", () => {
  const failure = runInNewContext(`${helpers}; discordFailure`, { t: (text: string) => text });
  expect(failure("detail", "This profile's speech setup cannot be reused.")).toBe("This profile's speech setup cannot be reused.");
  expect(failure("profile_exists")).toBe("A profile with that name already exists; choose another name.");
  expect(failure("unknown")).toBe("That didn't work. Try again.");
});
