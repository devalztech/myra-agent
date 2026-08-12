import type { AuthResponse, LoginPayload, RegisterPayload } from "@/types";
// import { apiRequest } from "./client";

/**
 * Frontend-only placeholders.
 * Replace the bodies with the commented `apiRequest` calls once FastAPI exists.
 */

const simulate = (ms = 700) => new Promise((resolve) => setTimeout(resolve, ms));

export async function login(payload: LoginPayload): Promise<AuthResponse> {
  // return apiRequest<AuthResponse>("/auth/login", { method: "POST", body: payload });
  await simulate();
  if (!payload.email || !payload.password) {
    throw new Error("Email and password are required.");
  }
  return {
    token: "frontend-placeholder-token",
    user: { id: "1", name: "Myra User", email: payload.email },
  };
}

export async function register(payload: RegisterPayload): Promise<AuthResponse> {
  // return apiRequest<AuthResponse>("/auth/register", { method: "POST", body: payload });
  await simulate();
  return {
    token: "frontend-placeholder-token",
    user: { id: "1", name: payload.name, email: payload.email },
  };
}
