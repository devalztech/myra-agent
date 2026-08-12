import { Link, useRouterState } from "@tanstack/react-router";
import { ChevronDown, Folder, Home, Menu, Moon, Settings, TerminalSquare, User, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { MyraLogo } from "./logo";
import { cn } from "@/lib/utils";

const navItems = [
  { label: "Home", to: "/" as const, icon: Home },
  { label: "Projects", to: null, icon: Folder },
  { label: "Terminal", to: null, icon: TerminalSquare },
  { label: "Settings", to: null, icon: Settings },
];

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });

  return (
    <div className="flex h-full flex-col">
      <div className="px-6 py-6">
        <MyraLogo />
      </div>

      <nav className="flex-1 px-3">
        <ul className="space-y-1">
          {navItems.map(({ label, to, icon: Icon }) => {
            const active = to !== null && pathname === to;
            const classes = cn(
              "flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-sm transition-colors",
              active
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-secondary hover:text-foreground",
            );
            return (
              <li key={label}>
                {to ? (
                  <Link to={to} onClick={onNavigate} className={classes}>
                    <Icon className="size-4" />
                    {label}
                  </Link>
                ) : (
                  <button type="button" className={classes}>
                    <Icon className="size-4" />
                    {label}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="border-t border-border px-4 py-4">
        <button
          type="button"
          className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm text-foreground transition-colors hover:bg-secondary"
        >
          <span className="flex size-7 items-center justify-center rounded-full bg-primary text-primary-foreground">
            <User className="size-4" />
          </span>
          <span className="flex-1 text-left">Myra User</span>
          <ChevronDown className="size-4 text-muted-foreground" />
        </button>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="hidden w-[17rem] shrink-0 border-r border-border bg-sidebar md:block">
        <SidebarContent />
      </aside>

      {open && (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-background/80"
            onClick={() => setOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 w-[17rem] border-r border-border bg-sidebar">
            <SidebarContent onNavigate={() => setOpen(false)} />
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between px-4 py-4 md:px-8">
          <button
            type="button"
            aria-label={open ? "Close menu" : "Open menu"}
            onClick={() => setOpen((v) => !v)}
            className="rounded-md p-2 text-muted-foreground transition-colors hover:text-foreground md:hidden"
          >
            {open ? <X className="size-5" /> : <Menu className="size-5" />}
          </button>
          <div className="flex-1" />
          <button
            type="button"
            aria-label="Theme"
            className="rounded-md p-2 text-muted-foreground transition-colors hover:text-foreground"
          >
            <Moon className="size-5" />
          </button>
        </header>

        <main className="flex-1">{children}</main>
      </div>
    </div>
  );
}
