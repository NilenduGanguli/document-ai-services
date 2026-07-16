import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useApiKey, useApiKeyPersist, useClientId, useTheme } from '../hooks/useSettings';
import {
  IconAdmin,
  IconDashboard,
  IconDocs,
  IconEye,
  IconEyeOff,
  IconFacts,
  IconJobs,
  IconLogo,
  IconMenu,
  IconMoon,
  IconSearch,
  IconSun,
  IconTree,
  IconUpload,
} from './Icons';

interface NavEntry {
  to: string;
  label: string;
  icon: JSX.Element;
  danger?: boolean;
}

const PRIMARY: NavEntry[] = [
  { to: '/', label: 'Dashboard', icon: <IconDashboard size={16} /> },
  { to: '/ingest', label: 'Ingest', icon: <IconUpload size={16} /> },
  { to: '/jobs', label: 'Jobs', icon: <IconJobs size={16} /> },
];

const KNOWLEDGE: NavEntry[] = [
  { to: '/documents', label: 'Documents', icon: <IconDocs size={16} /> },
  { to: '/tree', label: 'Knowledge tree', icon: <IconTree size={16} /> },
  { to: '/facts', label: 'Facts', icon: <IconFacts size={16} /> },
  { to: '/search', label: 'Search', icon: <IconSearch size={16} /> },
];

const SYSTEM: NavEntry[] = [{ to: '/admin', label: 'Admin', icon: <IconAdmin size={16} />, danger: true }];

function NavGroup({
  label,
  entries,
  onNavigate,
}: {
  label: string;
  entries: NavEntry[];
  onNavigate: () => void;
}): JSX.Element {
  return (
    <>
      <div className="nav-label">{label}</div>
      {entries.map((e) => (
        <NavLink
          key={e.to}
          to={e.to}
          end={e.to === '/'}
          onClick={onNavigate}
          className={({ isActive }) =>
            `nav-item${isActive ? ' active' : ''}${e.danger ? ' danger' : ''}`
          }
        >
          {e.icon}
          {e.label}
        </NavLink>
      ))}
    </>
  );
}

/** Header controls: client id + API key + theme. */
function HeaderBar({ onToggleSidebar }: { onToggleSidebar: () => void }): JSX.Element {
  const [clientId, setClientId] = useClientId();
  const [apiKey, setApiKey] = useApiKey();
  const [apiKeyPersist, setApiKeyPersist] = useApiKeyPersist();
  const { theme, toggle } = useTheme();
  const [showKey, setShowKey] = useState(false);

  return (
    <header className="topbar">
      <button
        type="button"
        className="btn btn-ghost btn-icon hamburger"
        onClick={onToggleSidebar}
        aria-label="Toggle navigation menu"
      >
        <IconMenu size={18} />
      </button>

      <label className="field">
        <span className="field-label" id="client-label">
          Client ID
        </span>
        <input
          className="input input-inline"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="acme-bank-001"
          spellCheck={false}
          autoComplete="off"
          aria-labelledby="client-label"
        />
      </label>

      <label className="field" style={{ minWidth: 190 }}>
        <span className="field-label" id="key-label">
          API key
        </span>
        <span className="key-wrap">
          <input
            className="input input-inline"
            type={showKey ? 'text' : 'password'}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="X-API-KEY"
            spellCheck={false}
            autoComplete="off"
            aria-labelledby="key-label"
          />
          <button
            type="button"
            className="key-peek"
            onClick={() => setShowKey((v) => !v)}
            aria-label={showKey ? 'Hide API key' : 'Show API key'}
          >
            {showKey ? <IconEyeOff size={14} /> : <IconEye size={14} />}
          </button>
        </span>
      </label>

      <label className="field-inline" title="Keep the key in this browser's storage after the tab closes">
        <input
          type="checkbox"
          checked={apiKeyPersist}
          onChange={(e) => setApiKeyPersist(e.target.checked)}
        />
        <span className="field-label">Remember key on this device</span>
      </label>

      <span className="topbar-spacer" />

      <button
        type="button"
        className="btn btn-ghost btn-icon"
        onClick={toggle}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
        title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
      >
        {theme === 'dark' ? <IconSun size={16} /> : <IconMoon size={16} />}
      </button>
    </header>
  );
}

export function Layout(): JSX.Element {
  const [open, setOpen] = useState(false);
  const location = useLocation();

  // Close the mobile drawer on navigation.
  useEffect(() => setOpen(false), [location.pathname]);

  return (
    <div className="shell">
      <nav className={`sidebar${open ? ' open' : ''}`} aria-label="Primary">
        <div className="sidebar-brand">
          <span className="logo-mark">
            <IconLogo size={19} />
          </span>
          <div>
            <div className="brand-title">Document Intelligence</div>
            <div className="brand-sub">KYC console</div>
          </div>
        </div>

        <div className="nav">
          <NavGroup label="Operate" entries={PRIMARY} onNavigate={() => setOpen(false)} />
          <NavGroup label="Knowledge" entries={KNOWLEDGE} onNavigate={() => setOpen(false)} />
          <NavGroup label="System" entries={SYSTEM} onNavigate={() => setOpen(false)} />
        </div>

        <div className="sidebar-foot">
          <span>v1.0.0</span>
          <span className="mono">/api/v1</span>
        </div>
      </nav>

      {open && <div className="scrim" onClick={() => setOpen(false)} aria-hidden />}

      <div className="main">
        <HeaderBar onToggleSidebar={() => setOpen((v) => !v)} />
        <Outlet />
      </div>
    </div>
  );
}
