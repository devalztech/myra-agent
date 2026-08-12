import type { ChatSession } from "@/types";
// import { apiRequest } from "./client";

/** Mock session data — replaced by GET /sessions when the backend lands. */
export const mockSessions: ChatSession[] = [
  {
    id: "s1",
    title: "Refactor auth middleware",
    updatedAt: "2m ago",
    messages: [
      {
        id: "m1",
        role: "user",
        content: "Split the auth middleware into a reusable dependency.",
        createdAt: "2m ago",
      },
      {
        id: "m2",
        role: "assistant",
        content:
          "I'd extract the token parsing into `get_current_user` and keep route handlers free of auth logic. Want me to sketch the module layout first?",
        createdAt: "2m ago",
      },
    ],
  },
  {
    id: "s2",
    title: "Dockerfile for Render",
    updatedAt: "1h ago",
    messages: [],
  },
  {
    id: "s3",
    title: "Vite build size audit",
    updatedAt: "Yesterday",
    messages: [],
  },
];

export async function fetchSessions(): Promise<ChatSession[]> {
  // return apiRequest<ChatSession[]>("/sessions");
  return mockSessions;
}
