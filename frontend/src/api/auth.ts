import type { AuthResponse, LoginPayload, RegisterPayload, User } from "@/types";
import { apiRequest } from "./client";

export function login(payload: LoginPayload): Promise<AuthResponse> {
  return apiRequest<AuthResponse>("/auth/login", {
    method: "POST",
    body: { email: payload.email.trim(), password: payload.password },
  });
}

export function register(payload: RegisterPayload): Promise<AuthResponse> {
  return apiRequest<AuthResponse>("/auth/register", {
    method: "POST",
    body: {
      name: payload.name.trim(),
      email: payload.email.trim(),
      password: payload.password,
    },
  });
}

export function fetchMe(token: string): Promise<User> {
  return apiRequest<User>("/auth/me", { token });
}
