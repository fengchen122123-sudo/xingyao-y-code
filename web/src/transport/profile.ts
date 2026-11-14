export type AgentProfile = "agent" | "desktop";

export function endpointFor(origin: string, profile: AgentProfile): string {
  const url = new URL(origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = profile === "desktop" ? "/relay/client" : "/ws/agent";
  url.search = "";
  url.hash = "";
  return url.toString();
}
