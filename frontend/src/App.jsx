import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Prism from "prismjs";
import "prismjs/components/prism-python.js";
import "prismjs/components/prism-javascript.js";
import "prismjs/components/prism-typescript.js";
import "prismjs/components/prism-json.js";
import "prismjs/components/prism-markup.js";
import "prismjs/components/prism-css.js";
import "prismjs/components/prism-sql.js";
import "prismjs/themes/prism-tomorrow.css";
import api, { getErrorMessage } from "./api.js";

const AGENTS = [
  { id: "project_manager", name: "Project Manager", role: "Planning", icon: "layers" },
  { id: "architect", name: "Software Architect", role: "System design", icon: "blueprint" },
  { id: "developer", name: "Developer", role: "Implementation", icon: "code" },
  { id: "tester", name: "Testing Agent", role: "Quality assurance", icon: "check" },
  { id: "reviewer", name: "Code Reviewer", role: "Review", icon: "scan" },
  { id: "debugger", name: "Debugger", role: "Diagnostics", icon: "bug" },
];

const NAV_ITEMS = [
  { id: "dashboard", label: "Overview", icon: "grid" },
  { id: "new-project", label: "New project", icon: "plus" },
];

const STAGE_AGENTS = {
  planning: "project_manager",
  architecture: "architect",
  development: "developer",
  testing: "tester",
  review: "reviewer",
  debugging: "debugger",
};

function Icon({ name, size = 18 }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
  };
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
    plus: <><path d="M12 5v14M5 12h14" /></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>,
    blueprint: <><path d="M4 4h16v16H4z" /><path d="M8 8h3v3H8zM15 8h1M15 12h1M8 15h8M8 18h4" /></>,
    code: <><path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14" /></>,
    check: <><path d="M20 6 9 17l-5-5" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h10" /></>,
    scan: <><path d="M4 7V5a1 1 0 0 1 1-1h2M17 4h2a1 1 0 0 1 1 1v2M20 17v2a1 1 0 0 1-1 1h-2M7 20H5a1 1 0 0 1-1-1v-2M7 12h10M12 7v10" /></>,
    bug: <><path d="M8 8h8v9a4 4 0 0 1-8 0V8Z" /><path d="M10 4 8 6M14 4l2 2M4 13h4M16 13h4M5 8l3 2M19 8l-3 2M5 18l3-2M19 18l-3-2" /></>,
    search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>,
    bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></>,
    key: <><circle cx="8" cy="15" r="5" /><path d="m11.5 11.5 9-9M17 6l2 2M14 9l2 2" /></>,
    arrow: <><path d="M5 12h14M13 6l6 6-6 6" /></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M5.6 9a7 7 0 0 1 11.6-2L20 12M4 12l2.8 5a7 7 0 0 0 11.6-2" /></>,
    folder: <><path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    close: <><path d="m18 6-12 12M6 6l12 12" /></>,
    chevron: <><path d="m9 18 6-6-6-6" /></>,
    branch: <><path d="M6 3v12M18 9a3 3 0 1 0-3-3v7a3 3 0 0 1-3 3H6" /><circle cx="6" cy="18" r="3" /></>,
    file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M8 13h8M8 17h8" /></>,
    shield: <><path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z" /><path d="m9 12 2 2 4-4" /></>,
  };
  return <svg {...common}>{paths[name] || paths.grid}</svg>;
}

function Badge({ children, tone = "neutral" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function StatusBadge({ status }) {
  const value = (status || "pending").toLowerCase();
  const tone = ["completed", "passed", "approved"].includes(value)
    ? "success"
    : ["failed", "rejected", "blocked"].includes(value)
      ? "danger"
      : ["in_progress", "awaiting_approval", "waiting_for_approval", "running", "pending"].includes(value)
        ? "warning"
        : "neutral";
  return <Badge tone={tone}>{value.replaceAll("_", " ")}</Badge>;
}

function EmptyState({ icon = "folder", title, detail, action }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon name={icon} size={22} /></div>
      <h3>{title}</h3>
      <p>{detail}</p>
      {action}
    </div>
  );
}

function Loading({ label = "Loading workspace" }) {
  return <div className="loading-state"><span className="spinner" />{label}</div>;
}

function Alert({ message, onDismiss }) {
  if (!message) return null;
  return (
    <div className="alert" role="alert">
      <Icon name="shield" size={17} />
      <span>{message}</span>
      {onDismiss && <button className="icon-button alert-close" onClick={onDismiss} aria-label="Dismiss"><Icon name="close" size={15} /></button>}
    </div>
  );
}

function App() {
  const [page, setPage] = useState("dashboard");
  const [projects, setProjects] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [project, setProject] = useState(null);
  const [workflow, setWorkflow] = useState(null);
  const [workflowDetails, setWorkflowDetails] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [files, setFiles] = useState([]);
  const [logs, setLogs] = useState([]);
  const [testRuns, setTestRuns] = useState([]);
  const [codeExecution, setCodeExecution] = useState(null);
  const [loading, setLoading] = useState(true);
  const [apiConnected, setApiConnected] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [query, setQuery] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [tokenDraft, setTokenDraft] = useState("");
  const [tokenSaved, setTokenSaved] = useState(() => Boolean(sessionStorage.getItem("devforge-api-token")));
  const [activeTab, setActiveTab] = useState("workspace");
  const toastTimer = useRef(null);

  const loadProjects = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError("");
    try {
      const all = [];
      let offset = 0;
      while (true) {
        const { data } = await api.get("/api/projects", { params: { limit: 200, offset } });
        all.push(...data);
        if (data.length < 200) break;
        offset += 200;
      }
      setProjects(all);
      setApiConnected(true);
    } catch (requestError) {
      setApiConnected(false);
      setError(getErrorMessage(requestError));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  const loadProjectData = useCallback(async (id, quiet = false) => {
    if (!id) return;
    if (!quiet) setDetailLoading(true);
    try {
      const [projectResponse, statusResponse, workflowResponse, taskResponse, fileResponse, logResponse, testResponse, executionResponse] = await Promise.all([
        api.get(`/api/projects/${id}`),
        api.get(`/api/projects/${id}/status`),
        api.get(`/api/projects/${id}/workflow`),
        api.get(`/api/projects/${id}/tasks`),
        api.get(`/api/projects/${id}/files`, { params: { include_history: true } }),
        api.get(`/api/projects/${id}/logs`),
        api.get(`/api/projects/${id}/tests`),
        api.get(`/api/projects/${id}/execution`).catch((requestError) => {
          if (requestError.response?.status === 404) return { data: null };
          throw requestError;
        }),
      ]);
      setProject(projectResponse.data);
      setWorkflow(statusResponse.data);
      setWorkflowDetails(workflowResponse.data);
      setTasks(taskResponse.data);
      setFiles(fileResponse.data);
      setLogs(logResponse.data);
      setTestRuns(testResponse.data);
      setCodeExecution(executionResponse.data);
      setProjects((current) => current.map((item) => item.id === id ? projectResponse.data : item));
      setError("");
    } catch (requestError) {
      setError(getErrorMessage(requestError));
    } finally {
      if (!quiet) setDetailLoading(false);
    }
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);

  const hasActiveProjects = projects.some((item) => ["in_progress", "awaiting_approval"].includes(item.status));
  useEffect(() => {
    if (page !== "dashboard" || !hasActiveProjects) return undefined;
    const timer = window.setInterval(() => loadProjects(true), 3500);
    return () => window.clearInterval(timer);
  }, [page, hasActiveProjects, loadProjects]);

  useEffect(() => {
    if (!selectedId) return;
    loadProjectData(selectedId);
  }, [selectedId, loadProjectData]);

  useEffect(() => {
    if (!selectedId || !["in_progress", "awaiting_approval"].includes(project?.status)) return undefined;
    const timer = window.setInterval(() => {
      loadProjectData(selectedId, true);
      loadProjects(true);
    }, 3500);
    return () => window.clearInterval(timer);
  }, [selectedId, project?.status, loadProjectData, loadProjects]);

  useEffect(() => {
    if (!selectedId || codeExecution?.status !== "running") return undefined;
    let active = true;
    const timer = window.setTimeout(async () => {
      try {
        const { data } = await api.get(`/api/projects/${selectedId}/execution`);
        if (active) setCodeExecution(data);
      } catch (requestError) {
        if (active) setError(getErrorMessage(requestError));
      }
    }, 1200);
    return () => { active = false; window.clearTimeout(timer); };
  }, [selectedId, codeExecution]);

  useEffect(() => () => window.clearTimeout(toastTimer.current), []);

  const notify = (message) => {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 3800);
  };

  const openProject = (id, tab = "workspace") => {
    if (id !== selectedId) setProject(null);
    setSelectedId(id);
    setActiveTab(tab);
    setPage("workspace");
    setError("");
  };

  const startProject = async (id) => {
    try {
      await api.post(`/api/projects/${id}/start`);
      notify("Workflow started. Project activity will update automatically.");
      await Promise.all([loadProjects(), loadProjectData(id, true)]);
    } catch (requestError) {
      setError(getErrorMessage(requestError));
    }
  };

  const submitApproval = async (approved) => {
    if (!selectedId) return;
    try {
      await api.post(`/api/projects/${selectedId}/approval`, { approved });
      notify(approved ? "Architecture approved. Development is resuming." : "Architecture rejected.");
      await Promise.all([loadProjects(), loadProjectData(selectedId, true)]);
    } catch (requestError) {
      setError(getErrorMessage(requestError));
    }
  };

  const executeProject = async (entrypoint) => {
    if (!selectedId) return;
    try {
      const { data } = await api.post(`/api/projects/${selectedId}/execute`, { entrypoint });
      setCodeExecution(data);
      notify("Execution started inside the Docker sandbox.");
    } catch (requestError) {
      setError(getErrorMessage(requestError));
    }
  };

  const createProject = async (payload) => {
    const { data } = await api.post("/api/projects", payload);
    await loadProjects();
    setSelectedId(data.id);
    setActiveTab("workspace");
    setPage("workspace");
    try {
      await api.post(`/api/projects/${data.id}/start`);
      notify("Project created. The workflow is running in the background.");
    } catch (requestError) {
      notify(`Project created. ${getErrorMessage(requestError)}`);
    }
    await loadProjectData(data.id, true);
  };

  const saveToken = () => {
    const value = tokenDraft.trim();
    if (value) {
      sessionStorage.setItem("devforge-api-token", value);
    } else {
      sessionStorage.removeItem("devforge-api-token");
    }
    setTokenDraft("");
    setTokenSaved(Boolean(value));
    setShowToken(false);
    loadProjects();
    if (selectedId) loadProjectData(selectedId, true);
    notify(value ? "API access token saved for this browser session." : "API access token removed.");
  };

  const filteredProjects = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return projects.filter((item) => !needle || `${item.name} ${item.description}`.toLowerCase().includes(needle));
  }, [projects, query]);

  const currentTitle = page === "dashboard"
    ? "Overview"
    : page === "new-project"
      ? "New project"
      : project?.name || "Project workspace";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => { setSelectedId(null); setPage("dashboard"); }} aria-label="DevForge AI home">
          <span className="brand-mark"><span /></span>
          <span className="brand-copy"><strong>devforge<span>.ai</span></strong><small>ENGINEERING OS</small></span>
        </button>

        <div className="workspace-picker">
          <span className="workspace-avatar">DF</span>
          <span><strong>Development</strong><small>Personal workspace</small></span>
          <span className="picker-dots">•••</span>
        </div>

        <div className="nav-label">WORKSPACE</div>
        <nav className="primary-nav">
          {NAV_ITEMS.map((item) => (
            <button key={item.id} className={`nav-item ${page === item.id ? "active" : ""}`} onClick={() => { setPage(item.id); setSelectedId(null); setError(""); }}>
              <Icon name={item.icon} size={17} /><span>{item.label}</span>
              {item.id === "new-project" && <span className="nav-shortcut">+</span>}
            </button>
          ))}
        </nav>

        <div className="nav-label projects-label">YOUR PROJECTS <span>{projects.length}</span></div>
        <div className="sidebar-projects">
          {projects.slice(0, 7).map((item, index) => (
            <button key={item.id} className={`project-nav-item ${selectedId === item.id ? "selected" : ""}`} onClick={() => openProject(item.id)}>
              <span className={`project-dot dot-${index % 5}`} />
              <span className="project-nav-name">{item.name}</span>
              <span className={`mini-status ${item.status === "completed" ? "done" : item.status === "failed" ? "failed" : item.status === "in_progress" || item.status === "awaiting_approval" ? "live" : ""}`} />
            </button>
          ))}
          {!projects.length && !loading && <div className="sidebar-empty">No projects yet</div>}
          {projects.length > 7 && <button className="see-all" onClick={() => setPage("dashboard")}>View all projects <Icon name="arrow" size={13} /></button>}
        </div>

        <div className="sidebar-bottom">
          <div className="system-card">
            <div className="system-card-top"><span className="pulse-dot" /> SYSTEM STATUS</div>
            <strong>{apiConnected === null ? "Checking API" : apiConnected ? "API connected" : "API unavailable"}</strong>
            <span>{apiConnected ? "Local development environment" : "Waiting for a successful API response"}</span>
          </div>
          <div className="profile-row">
            <span className="profile-avatar">JD</span>
            <span><strong>Developer</strong><small>Local account</small></span>
            <button className="icon-button" title="Configure API access token" onClick={() => setShowToken(true)}><Icon name="key" size={16} /></button>
          </div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>DevForge</span><Icon name="chevron" size={13} /><strong>{currentTitle}</strong></div>
          <div className="topbar-actions">
            <div className={`api-indicator ${apiConnected === false ? "api-offline" : ""}`}><span className={`pulse-dot ${apiConnected === false ? "offline-dot" : ""}`} /> {apiConnected === null ? "API checking" : apiConnected ? "API online" : "API unavailable"}</div>
            <button className="icon-button" title="Refresh projects" onClick={() => { loadProjects(); if (selectedId) loadProjectData(selectedId); }}><Icon name="refresh" size={17} /></button>
            <button className="icon-button token-button" title="Configure API access token" onClick={() => setShowToken(true)}><Icon name="key" size={17} /></button>
            <span className="topbar-divider" />
            <button className="avatar-button" title="Local developer profile">JD</button>
          </div>
        </header>

        <div className="page-content">
          <Alert message={error} onDismiss={() => setError("")} />
          {page === "dashboard" && (
            <DashboardPage
              projects={projects}
              loading={loading}
              onCreate={() => { setPage("new-project"); setError(""); }}
              onOpen={openProject}
              onRefresh={() => loadProjects(true)}
              query={query}
              setQuery={setQuery}
              filteredProjects={filteredProjects}
            />
          )}
          {page === "new-project" && (
            <NewProjectPage onCancel={() => setPage("dashboard")} onCreate={createProject} />
          )}
          {page === "workspace" && (
            <ProjectWorkspace
              project={project}
              workflow={workflow}
              workflowDetails={workflowDetails}
              codeExecution={codeExecution}
              tasks={tasks}
              files={files}
              logs={logs}
              testRuns={testRuns}
              loading={detailLoading}
              activeTab={activeTab}
              setActiveTab={setActiveTab}
              onStart={startProject}
              onApproval={submitApproval}
              onExecute={executeProject}
              onBack={() => { setSelectedId(null); setPage("dashboard"); }}
              apiError={error}
            />
          )}
        </div>
        <footer className="footer"><span>DEVFORGE AI <span className="footer-sep">/</span> MULTI-AGENT ENGINEERING</span><span>Built for thoughtful software development <span className="footer-heart">✳</span></span></footer>
      </main>

      {toast && <div className="toast"><span className="toast-check">✓</span>{toast}</div>}
      {showToken && (
        <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowToken(false); }}>
          <section className="token-modal">
            <div className="modal-top"><span className="modal-icon"><Icon name="key" size={19} /></span><button className="icon-button" onClick={() => setShowToken(false)} aria-label="Close"><Icon name="close" /></button></div>
            <h2>API access</h2>
            <p>If the backend has <code>API_ACCESS_TOKEN</code> configured, paste the access token here. It stays in this browser session only.</p>
            <label className="field-label" htmlFor="api-token">Access token</label>
            <input id="api-token" type="password" autoComplete="off" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder={tokenSaved ? "Token saved for this session" : "Paste token"} onKeyDown={(event) => { if (event.key === "Enter") saveToken(); }} />
            <div className="modal-actions"><button className="button button-quiet" onClick={() => { setTokenDraft(""); saveToken(); }}>Remove token</button><button className="button button-primary" onClick={saveToken}>Save token <Icon name="arrow" size={15} /></button></div>
          </section>
        </div>
      )}
    </div>
  );
}

function DashboardPage({ projects, loading, onCreate, onOpen, onRefresh, query, setQuery, filteredProjects }) {
  const total = projects.length;
  const active = projects.filter((item) => ["in_progress", "awaiting_approval"].includes(item.status)).length;
  const completed = projects.filter((item) => item.status === "completed").length;
  const failed = projects.filter((item) => item.status === "failed").length;
  const recent = useMemo(() => [...projects].sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at)).slice(0, 5), [projects]);
  const [activity, setActivity] = useState([]);
  const [activityError, setActivityError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function fetchActivity() {
      if (!recent.length) { setActivity([]); return; }
      try {
        const responses = await Promise.all(
          recent.map((item) => api.get(`/api/projects/${item.id}/logs`)),
        );
        if (!cancelled) {
          setActivity(
            responses
              .flatMap((response, index) =>
                response.data.map((event) => ({
                  ...event,
                  projectName: recent[index].name,
                  projectId: recent[index].id,
                })),
              )
              .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))
              .slice(0, 5),
          );
          setActivityError("");
        }
      } catch (error) {
        if (!cancelled) setActivityError(getErrorMessage(error));
      }
    }
    fetchActivity();
    return () => { cancelled = true; };
  }, [recent]);

  return (
    <>
      <div className="welcome-row">
        <div><div className="eyebrow"><span className="eyebrow-line" /> YOUR ENGINEERING COMMAND CENTER</div><h1>Good to see you<span className="title-period">.</span></h1><p className="page-subtitle">A clear view of your projects, agents, and development progress.</p></div>
        <button className="button button-primary" onClick={onCreate}><Icon name="plus" size={16} /> New project</button>
      </div>

      <section className="stats-grid">
        <StatCard label="Total projects" value={total} icon="folder" accent="blue" note="Across your workspace" />
        <StatCard label="Active projects" value={active} icon="clock" accent="violet" note="Currently in development" />
        <StatCard label="Completed" value={completed} icon="check" accent="green" note="Successfully delivered" />
        <StatCard label="Failed" value={failed} icon="bug" accent="red" note="Need your attention" />
      </section>

      <div className="dashboard-grid">
        <section className="panel projects-panel">
          <div className="panel-heading">
            <div><div className="panel-kicker">WORKSPACE</div><h2>All projects <span className="count-pill">{total}</span></h2></div>
            <div className="project-toolbar">
              <label className="search-field"><Icon name="search" size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search projects" /></label>
              <button className="icon-button small-refresh" onClick={onRefresh} title="Refresh projects"><Icon name="refresh" size={15} /></button>
            </div>
          </div>
          {loading ? <Loading label="Loading projects" /> : filteredProjects.length === 0 ? (
            <EmptyState icon="folder" title={total ? "No matching projects" : "Your workspace is ready"} detail={total ? "Try another search term." : "Create your first project and let the agents help shape the work."} action={!total && <button className="button button-secondary" onClick={onCreate}>Create a project <Icon name="arrow" size={14} /></button>} />
          ) : (
            <div className="project-table-wrap">
              <table className="project-table">
                <thead><tr><th>PROJECT</th><th>STATUS</th><th>LAST UPDATED</th><th /></tr></thead>
                <tbody>{filteredProjects.map((item, index) => (
                  <tr key={item.id} onClick={() => onOpen(item.id)} className="project-row">
                    <td><div className="project-cell"><span className={`project-tile tile-${index % 5}`}>{item.name.slice(0, 1).toUpperCase()}</span><span className="project-cell-info"><strong>{item.name}</strong><small>{truncate(item.description, 54)}</small></span></div></td>
                    <td><StatusBadge status={item.status} /></td>
                    <td><span className="date-cell">{formatRelative(item.updated_at)}</span></td>
                    <td><button className="row-action" aria-label={`Open ${item.name}`} onClick={(event) => { event.stopPropagation(); onOpen(item.id); }}><Icon name="chevron" size={16} /></button></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <div className="panel-footer"><span>Showing {filteredProjects.length} of {total} projects</span><span className="live-refresh"><span className="pulse-dot" /> Live updates enabled</span></div>
        </section>

        <aside className="side-stack">
          <section className="panel activity-panel">
            <div className="panel-heading compact"><div><div className="panel-kicker">WHAT'S HAPPENING</div><h2>Recent activity</h2></div><span className="activity-spark">↗</span></div>
            {!projects.length ? <div className="activity-empty">Activity appears here once a workflow starts.</div> : activityError ? <div className="inline-error">{activityError}</div> : activity.length ? (
              <div className="activity-list">{activity.map((item, index) => (
                <button className="activity-item activity-button" key={`${item.projectId}-${item.timestamp}-${index}`} onClick={() => onOpen(item.projectId, "agents")}><span className={`activity-dot activity-${index % 3}`} /><div className="activity-copy"><strong>{friendlyAgent(item.agent_name)} <span className="activity-project">{item.projectName}</span></strong><span>{item.message}</span><small>{formatRelative(item.timestamp)}</small></div><StatusBadge status={item.status} /></button>
              ))}</div>
            ) : <div className="activity-empty">{recent.some((item) => item.status !== "pending") ? "No workflow events available yet." : "Create or start a project to see activity."}</div>}
            {projects.length > 0 && <button className="text-link" onClick={() => onOpen(projects[0].id, "agents")}>View agent activity <Icon name="arrow" size={14} /></button>}
          </section>

          <section className="agent-summary-card">
            <div className="agent-summary-top"><div><div className="panel-kicker">YOUR AI TEAM</div><h2>Built to collaborate</h2></div><div className="agent-orbit"><span /><span /><span /><span /><span /><span /></div></div>
            <p>Six specialist agents. One connected development workflow.</p>
            <div className="agent-chip-row">{AGENTS.map((agent) => <span className="agent-chip" title={agent.name} key={agent.id}><Icon name={agent.icon} size={14} /></span>)}</div>
            <div className="team-caption"><span className="pulse-dot" /> Ready when you are</div>
          </section>
        </aside>
      </div>
      <div className="dashboard-bottom-note"><Icon name="shield" size={15} /><span>Your generated code is reviewed and tested in an isolated workflow before delivery.</span><span className="note-link">Learn about the workflow <Icon name="arrow" size={13} /></span></div>
    </>
  );
}

function StatCard({ label, value, icon, accent, note }) {
  return <article className={`stat-card stat-${accent}`}><div className="stat-top"><span>{label}</span><span className="stat-icon"><Icon name={icon} size={17} /></span></div><div className="stat-value">{value}</div><div className="stat-note"><span className={`stat-mark mark-${accent}`} />{note}</div></article>;
}

function NewProjectPage({ onCancel, onCreate }) {
  const [form, setForm] = useState({ project_name: "", description: "", requirements: "", programming_language: "Python", preferred_framework: "" });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event) => {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await onCreate({
        project_name: form.project_name.trim(),
        description: form.description.trim(),
        requirements: form.requirements.split("\n").map((line) => line.trim()).filter(Boolean),
        programming_language: form.programming_language.trim(),
        preferred_framework: form.preferred_framework.trim() || null,
      });
    } catch (requestError) {
      setError(getErrorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="new-project-layout">
      <div className="form-page-heading"><button className="back-link" onClick={onCancel}>← Back to overview</button><div className="eyebrow"><span className="eyebrow-line" /> START SOMETHING GREAT</div><h1>New project<span className="title-period">.</span></h1><p className="page-subtitle">Give your agent team a clear brief. You’ll review the architecture before any code is generated.</p></div>
      {error && <Alert message={error} onDismiss={() => setError("")} />}
      <div className="new-project-grid">
        <form className="panel project-form" onSubmit={submit}>
          <div className="form-section-title"><span className="form-step">01</span><div><h2>Project brief</h2><p>Tell us what you want to build.</p></div></div>
          <div className="form-field"><label htmlFor="project-name">Project name <span>*</span></label><input id="project-name" required maxLength={200} value={form.project_name} onChange={(event) => update("project_name", event.target.value)} placeholder="e.g. Invoice automation tool" /><small>Choose a memorable name for your workspace.</small></div>
          <div className="form-field"><label htmlFor="project-description">Description <span>*</span></label><textarea id="project-description" required rows={4} value={form.description} onChange={(event) => update("description", event.target.value)} placeholder="What are you building? Who is it for? What problem does it solve?" /></div>
          <div className="form-field"><label htmlFor="project-requirements">Requirements <span className="optional">(one per line)</span></label><textarea id="project-requirements" rows={6} value={form.requirements} onChange={(event) => update("requirements", event.target.value)} placeholder={"Users can create and manage invoices\nExport invoices as PDF\nTrack payment status"} /><small>Specific requirements help the agents plan more accurately.</small></div>
          <div className="form-two-col">
            <div className="form-field"><label htmlFor="programming-language">Programming language <span>*</span></label><select id="programming-language" required value={form.programming_language} onChange={(event) => update("programming_language", event.target.value)}><option>Python</option></select><small>Python is currently supported by sandboxed testing and execution.</small></div>
            <div className="form-field"><label htmlFor="preferred-framework">Technology preference <span className="optional">(optional)</span></label><input id="preferred-framework" value={form.preferred_framework} onChange={(event) => update("preferred_framework", event.target.value)} placeholder="Preferred framework, e.g. FastAPI" /><small>The current API accepts one preferred_framework value.</small></div>
          </div>
          <div className="form-footer"><span className="required-note"><span>*</span> Required fields</span><div><button type="button" className="button button-quiet" onClick={onCancel}>Cancel</button><button className="button button-primary" disabled={submitting}>{submitting ? <><span className="button-spinner" /> Creating…</> : <>Create & start <Icon name="arrow" size={15} /></>}</button></div></div>
        </form>

        <aside className="project-intro-rail">
          <div className="intro-card intro-glow"><div className="intro-graphic"><span className="graphic-ring ring-one" /><span className="graphic-ring ring-two" /><span className="graphic-core"><Icon name="layers" size={23} /></span><span className="graphic-node node-one" /><span className="graphic-node node-two" /><span className="graphic-node node-three" /></div><div className="panel-kicker">HOW IT WORKS</div><h2>From brief to build, thoughtfully.</h2><p>Your project moves through a structured review process. You stay in control at every important decision.</p><div className="intro-steps"><span><i>01</i> Plan & design</span><span><i>02</i> You approve architecture</span><span><i>03</i> Build, test & review</span></div></div>
          <div className="intro-note"><Icon name="shield" size={16} /><span>Code generation only begins after you approve the proposed architecture.</span></div>
        </aside>
      </div>
    </div>
  );
}

function ProjectWorkspace({ project, workflow, workflowDetails, codeExecution, tasks, files, logs, testRuns, loading, activeTab, setActiveTab, onStart, onApproval, onExecute, onBack, apiError }) {
  const [selectedFile, setSelectedFile] = useState(null);
  const latestFiles = useMemo(() => {
    const latest = new Map();
    for (const file of files) {
      if (!latest.has(file.file_path) || latest.get(file.file_path).version < file.version) {
        latest.set(file.file_path, file);
      }
    }
    return [...latest.values()];
  }, [files]);
  useEffect(() => {
    setSelectedFile((current) =>
      files.find((file) => file.file_path === current?.file_path && file.version === current?.version)
      || latestFiles.find((file) => file.file_path === current?.file_path)
      || latestFiles[0]
      || null,
    );
  }, [files, latestFiles]);

  if (loading && !project) return <Loading />;
  if (!project) return <EmptyState title="Project unavailable" detail={apiError || "This project could not be loaded."} action={<button className="button button-secondary" onClick={onBack}>Back to overview</button>} />;

  const currentStage = workflow?.current_stage || null;
  const displayStage = currentStage === "intake" ? "planning" : currentStage;
  const runningAgent = STAGE_AGENTS[displayStage];
  const approvalPending = project.status === "awaiting_approval" && workflow?.architecture_design;
  const tabs = [
    { id: "workspace", label: "Workspace" },
    { id: "workflow", label: "Live workflow" },
    { id: "agents", label: "Agent activity", count: logs.length },
    { id: "code", label: "Code", count: files.length },
    { id: "testing", label: "Testing & review" },
  ];

  return (
    <>
      <button className="back-link workspace-back" onClick={onBack}>← All projects</button>
      <div className="workspace-heading">
        <div className="workspace-title-group"><span className="project-tile tile-0 large-tile">{project.name.slice(0, 1).toUpperCase()}</span><div><div className="eyebrow"><span className="eyebrow-line" /> PROJECT WORKSPACE</div><h1>{project.name}<span className="title-period">.</span></h1><p>{project.description}</p></div></div>
        <div className="workspace-actions"><StatusBadge status={project.status} />{project.status === "pending" && <button className="button button-primary" onClick={() => onStart(project.id)}>Start development <Icon name="arrow" size={15} /></button>}<button className="icon-button" title="Refresh workspace" onClick={() => window.location.reload()}><Icon name="refresh" size={16} /></button></div>
      </div>

      <section className="workspace-progress panel">
        <div className="progress-overview"><div><div className="panel-kicker">CURRENT WORKFLOW STAGE</div><h2>{formatStage(displayStage || project.status)}</h2><p>{workflow?.workflow_summary || stageDescription(displayStage, project.status)}</p></div><div className="progress-orb"><span className={project.status === "in_progress" ? "orb-running" : ""}>{project.status === "completed" ? "✓" : project.status === "failed" ? "!" : "✳"}</span></div></div>
        <div className="workflow-steps">{["planning", "architecture", "development", "testing", "review"].map((stage, index) => { const position = ["planning", "architecture", "development", "testing", "review", "debugging", "complete"].indexOf(displayStage); const activeIndex = project.status === "completed" ? 5 : position; const done = activeIndex > index; return <div className={`workflow-step ${done ? "step-done" : ""} ${displayStage === stage ? "step-active" : ""}`} key={stage}><span className="step-marker">{done ? "✓" : String(index + 1).padStart(2, "0")}</span><span>{formatStage(stage)}</span></div>; })}</div>
      </section>

      {approvalPending && <section className="approval-card"><div className="approval-icon"><Icon name="blueprint" size={22} /></div><div className="approval-copy"><Badge tone="warning">ACTION REQUIRED</Badge><h2>Architecture ready for your review</h2><p>Review the proposed design below. Development starts only after you approve. Rejecting will stop this workflow without generating code.</p></div><div className="approval-actions"><button className="button button-quiet" onClick={() => onApproval(false)}>Reject</button><button className="button button-primary" onClick={() => onApproval(true)}><Icon name="check" size={15} /> Approve architecture</button></div></section>}

      <div className="workspace-tabs" role="tablist">{tabs.map((tab) => <button key={tab.id} role="tab" aria-selected={activeTab === tab.id} className={`workspace-tab ${activeTab === tab.id ? "selected" : ""}`} onClick={() => setActiveTab(tab.id)}>{tab.label}{tab.count > 0 && <span>{tab.count}</span>}</button>)}</div>

      {activeTab === "workspace" && <WorkspaceTab project={project} workflow={workflow} workflowDetails={workflowDetails} tasks={tasks} logs={logs} files={files} testRuns={testRuns} runningAgent={runningAgent} approvalPending={approvalPending} setActiveTab={setActiveTab} />}
      {activeTab === "workflow" && <WorkflowTab workflow={workflow} details={workflowDetails} logs={logs} />}
      {activeTab === "agents" && <AgentActivityTab logs={logs} currentStage={displayStage} projectStatus={project.status} details={workflowDetails} />}
      {activeTab === "code" && <CodeTab files={files} selectedFile={selectedFile} setSelectedFile={setSelectedFile} project={project} workflow={workflowDetails} execution={codeExecution} onExecute={onExecute} />}
      {activeTab === "testing" && <TestingTab runs={testRuns} workflow={workflow} details={workflowDetails} execution={codeExecution} project={project} />}
    </>
  );
}

function WorkspaceTab({ project, workflow, workflowDetails, tasks, logs, files, testRuns, runningAgent, approvalPending, setActiveTab }) {
  const architecture = workflow?.architecture_design;
  const latest = testRuns[testRuns.length - 1];
  return (
    <>
      <div className="workspace-columns">
        <div className="workspace-main-column">
          <section className="panel detail-panel"><PanelTitle kicker="PROJECT BRIEF" title="Requirements" icon="layers" /><div className="requirements-summary"><p>{project.description}</p>{workflowDetails?.requirements?.length ? <ul className="requirement-list">{workflowDetails.requirements.map((requirement, index) => <li key={`${index}-${requirement}`}>{requirement}</li>)}</ul> : <div className="requirement-meta"><span><i className="meta-dot" /> No functional requirements were recorded.</span></div>}<div className="requirement-meta"><span><i className="meta-dot" />{workflowDetails?.programming_language || "Language not reported"}{workflowDetails?.preferred_framework ? ` · ${workflowDetails.preferred_framework}` : ""}</span></div></div></section>
          <section className="panel detail-panel"><PanelTitle kicker="SYSTEM DESIGN" title="Architecture overview" icon="blueprint" /><ArchitectureSummary architecture={architecture} approvalPending={approvalPending} /></section>
          <section className="panel detail-panel"><PanelTitle kicker="EXECUTION PLAN" title="Development tasks" icon="check" count={tasks.length} />{tasks.length ? <TaskList tasks={tasks} /> : <InlineEmpty text="Development tasks will appear when the planning agent completes." />}</section>
        </div>
        <aside className="workspace-rail">
          <section className="panel agent-glance"><PanelTitle kicker="YOUR AI TEAM" title="Agents" icon="layers" /><div className="agent-glance-list">{AGENTS.map((agent) => <div className="agent-glance-item" key={agent.id}><span className={`agent-avatar agent-${agent.id}`}><Icon name={agent.icon} size={15} /></span><span className="agent-glance-copy"><strong>{agent.name}</strong><small>{agent.role}</small></span>{agent.id === runningAgent ? <Badge tone="warning">Working</Badge> : <span className="agent-quiet-state">{project.status === "completed" ? "Done" : project.status === "failed" ? "Stopped" : "Queued"}</span>}</div>)}</div></section>
          <section className="panel mini-card"><PanelTitle kicker="LATEST RUN" title="Test results" icon="check" />{latest ? <div className="test-mini"><div className="test-mini-stats"><strong className="text-green">{latest.passed_tests}<small> passed</small></strong><strong className="text-red">{latest.failed_tests}<small> failed</small></strong></div><StatusBadge status={latest.execution_status} /><small>{formatDate(latest.timestamp)}</small></div> : <InlineEmpty text="No test run recorded yet." />}</section>
          <section className="panel mini-card"><PanelTitle kicker="PROJECT OUTPUT" title="Generated files" icon="folder" count={files.length} />{files.length ? <div className="file-short-list">{files.slice(0, 4).map((file) => <div className="file-short" key={`${file.file_path}-${file.version}`}><Icon name="file" size={14} /><span>{file.file_path}</span></div>)}</div> : <InlineEmpty text="Files appear as agents generate the project." />}</section>
          <section className="panel mini-card"><PanelTitle kicker="ACTIVITY" title="Agent executions" icon="clock" count={logs.length} />{logs.length ? <div className="recent-log-list">{logs.slice(-3).reverse().map((log, index) => <div className="recent-log" key={`${log.timestamp}-${index}`}><span className="pulse-dot" /><span>{log.message}</span><small>{formatRelative(log.timestamp)}</small></div>)}</div> : <InlineEmpty text="No workflow activity yet." />}</section>
        </aside>
      </div>
    {project.status === "completed" && <FinalReport project={project} workflow={workflow} details={workflowDetails} files={files} onExplore={() => setActiveTab("code")} />}
    </>
  );
}

function WorkflowTab({ workflow, details, logs }) {
  const stage = workflow?.current_stage;
  const projectStatus = workflow?.status;
  const tasks = details?.development_tasks || [];
  const files = details?.generated_files || [];
  const tests = details?.test_results || [];
  const reviews = details?.code_review_results || [];
  const attempts = details?.debugging_attempts || [];
  const latestTest = tests.at(-1);
  const latestTestFailed = latestTest && (
    latestTest.failed_tests > 0
    || Boolean(latestTest.execution_error)
    || Boolean(latestTest.execution_status && latestTest.execution_status !== "passed")
  );
  const orderedStages = ["planning", "architecture", "development", "testing", "review", "debugging", "complete"];
  const stageIndex = orderedStages.indexOf(stage);
  const atStage = (name) => [...logs].reverse().find((event) => event.message.includes(`Workflow stage: ${name}.`));
  const stateFor = (name) => {
    if (name === "planning") {
      if (tasks.length || stageIndex >= 1) return "completed";
      if (stage === "planning") return projectStatus === "failed" ? "failed" : "running";
      return stage === "intake" && projectStatus === "in_progress" ? "running" : "pending";
    }
    if (name === "architect") {
      if (details?.architecture_design || stageIndex > 1) return "completed";
      if (stage === "architecture") return projectStatus === "failed" ? "failed" : "running";
      return "pending";
    }
    if (name === "approval") {
      if (workflow?.human_approval_status === "approved") return "completed";
      if (workflow?.human_approval_status === "rejected" || projectStatus === "blocked") return "failed";
      if (projectStatus === "awaiting_approval") return "waiting_for_approval";
      return "pending";
    }
    if (name === "developer") {
      if (stageIndex > 2) return "completed";
      if (stage === "development") {
        if (projectStatus === "failed") return "failed";
        if (projectStatus === "in_progress") {
          return files.length && tasks.every((task) => task.status === "completed") ? "completed" : "running";
        }
      }
      return "pending";
    }
    if (name === "file_manager") {
      if (files.length) return "completed";
      if (stage === "development") return projectStatus === "failed" ? "failed" : "running";
      return "pending";
    }
    if (name === "tester") {
      if (latestTest) return latestTestFailed ? "failed" : "completed";
      if (stageIndex > 3) return "completed";
      if (stage === "testing") return projectStatus === "failed" ? "failed" : "running";
      return "pending";
    }
    if (name === "reviewer") {
      if (reviews.length || stageIndex > 4) return "completed";
      if (stage === "review") return projectStatus === "failed" ? "failed" : "running";
      return "pending";
    }
    if (name === "debugger") {
      if (attempts.length) return "completed";
      if (stageIndex > 5) return "completed";
      if (stage === "debugging") return projectStatus === "failed" ? "failed" : "running";
      return "pending";
    }
    if (name === "complete") {
      if (projectStatus === "completed") return "completed";
      if (["failed", "blocked"].includes(projectStatus)) return "failed";
      return "pending";
    }
    return "pending";
  };
  const nodes = [
    { id: "planning", label: "Project Manager Agent", summary: `${tasks.length} planned task${tasks.length === 1 ? "" : "s"}`, stage: "architecture" },
    { id: "architect", label: "Software Architect Agent", summary: details?.architecture_design?.technology_stack?.join(" · ") || "Architecture design", stage: "architecture" },
    { id: "approval", label: "Human Approval", summary: workflow?.human_approval_status || "Approval gate", stage: "architecture" },
    { id: "developer", label: "Developer Agent", summary: `${files.length} generated file${files.length === 1 ? "" : "s"}`, stage: "development" },
    { id: "file_manager", label: "File Manager", summary: `${files.length} persisted file${files.length === 1 ? "" : "s"}`, stage: "development" },
    { id: "tester", label: "Testing Agent · Docker", summary: tests.at(-1)?.execution_status || "Isolated pytest run", stage: "testing" },
    { id: "reviewer", label: "Code Reviewer Agent", summary: reviews.at(-1) ? `Score ${reviews.at(-1).quality_score}/100` : "Independent quality review", stage: "review" },
    { id: "debugger", label: "Debugger Agent", summary: attempts.length ? `${attempts.length}/${details.max_debugging_attempts} attempts · ${attempts.at(-1).hypothesis}` : "Runs only when review/test gates fail", stage: "debugging" },
    { id: "complete", label: "Final Completion", summary: workflow?.workflow_summary || "Final gates must pass", stage: "complete" },
  ];
  return (
    <div className="workflow-page">
      <section className="panel workflow-panel">
        <div className="workflow-panel-heading"><PanelTitle kicker="LANGGRAPH CHECKPOINT STATE" title="Live development workflow" icon="branch" /><StatusBadge status={projectStatus} /></div>
        <p className="panel-description">Statuses come from the persisted workflow snapshot and recorded checkpoint events. Cards remain pending until an output or stage confirms progress.</p>
        <div className="workflow-node-list">{nodes.map((node, index) => {
          const nodeStatus = stateFor(node.id);
          const event = atStage(node.stage);
          const latest = node.id === "tester" ? tests.at(-1) : node.id === "reviewer" ? reviews.at(-1) : node.id === "debugger" ? attempts.at(-1) : null;
          const summary = node.id === "architect" ? (details?.architecture_design?.design_decisions?.join(" ") || node.summary) : node.id === "tester" ? latest ? `${latest.passed_tests} passed · ${latest.failed_tests} failed · ${latest.execution_status}` : node.summary : node.id === "reviewer" ? latest?.approval_rationale || node.summary : node.id === "debugger" ? latest?.outcome || node.summary : node.summary;
          return <div className={`workflow-node-wrap ${index < nodes.length - 1 ? "has-next" : ""}`} key={node.id}>
            <article className={`workflow-node node-${nodeStatus}`}>
              <span className="workflow-node-marker">{nodeStatus === "completed" ? "✓" : nodeStatus === "failed" ? "!" : String(index + 1).padStart(2, "0")}</span>
              <div className="workflow-node-copy"><div className="workflow-node-heading"><strong>{node.label}</strong><StatusBadge status={nodeStatus} /></div><p>{summary}</p>{event && <small>Latest checkpoint: {formatDate(event.timestamp)}</small>}{node.id === "debugger" && latest && <small>Root cause: {latest.hypothesis} · Proposed files: {(latest.corrected_files || []).join(", ") || "not reported"} · Retesting required</small>}</div>
            </article>
          </div>;
        })}</div>
      </section>
      {workflow?.architecture_design && <section className="panel detail-panel"><PanelTitle kicker="PROPOSED DESIGN" title="Architecture" icon="blueprint" /><ArchitectureSummary architecture={workflow.architecture_design} approvalPending={projectStatus === "awaiting_approval"} /></section>}
      {details?.error_messages?.length > 0 && <section className="panel error-panel"><PanelTitle kicker="WORKFLOW ERRORS" title="Recorded failures" icon="bug" />{details.error_messages.map((message, index) => <p key={`${index}-${message}`}>{message}</p>)}</section>}
    </div>
  );
}

function PanelTitle({ kicker, title, icon, count }) {
  return <div className="section-title"><span className="section-title-icon"><Icon name={icon} size={16} /></span><div><div className="panel-kicker">{kicker}</div><h2>{title}{count !== undefined && <span className="count-pill">{count}</span>}</h2></div></div>;
}

function ArchitectureSummary({ architecture, approvalPending }) {
  if (!architecture) return <InlineEmpty text="The architecture agent has not produced a design yet." />;
  return (
    <div className="architecture-summary">
      {approvalPending && <div className="approval-hint"><span className="pulse-dot" /> This proposal is waiting for your approval.</div>}
      <div className="architecture-tags">{architecture.technology_stack?.map((technology) => <Badge key={technology} tone="blue">{technology}</Badge>)}</div>
      {architecture.application_architecture?.length > 0 && <div className="architecture-components">{architecture.application_architecture.map((item) => <div className="architecture-component" key={item.name}><strong>{item.name}</strong><p>{item.responsibility}</p>{item.dependencies?.length > 0 && <small>Depends on {item.dependencies.join(", ")}</small>}</div>)}</div>}
      {architecture.design_decisions?.length > 0 && <div className="architecture-decisions"><h3>Design decisions</h3><ul>{architecture.design_decisions.map((decision, index) => <li key={`${index}-${decision}`}>{decision}</li>)}</ul></div>}
      {architecture.project_structure?.length > 0 && <div className="architecture-tree"><h3>Proposed structure</h3><pre>{architecture.project_structure.join("\n")}</pre></div>}
      {architecture.api_endpoints?.length > 0 && <div className="endpoint-list"><h3>API surface</h3>{architecture.api_endpoints.map((endpoint, index) => <div key={`${endpoint.method}-${endpoint.path}-${index}`}><Badge tone="violet">{endpoint.method}</Badge><code>{endpoint.path}</code><span>{endpoint.purpose}</span></div>)}</div>}
      {architecture.database_design && <div className="database-design"><h3>Database · {architecture.database_design.database_engine}</h3><div>{architecture.database_design.entities?.map((entity) => <article className="database-entity" key={entity.name}><strong>{entity.name}</strong><span>{entity.fields.join(" · ")}</span>{entity.relationships?.length > 0 && <small>{entity.relationships.join(" · ")}</small>}</article>)}</div>{architecture.database_design.design_notes?.map((note, index) => <p key={`${index}-${note}`}>{note}</p>)}</div>}
      {architecture.requirement_coverage?.length > 0 && <div className="architecture-decisions"><h3>Requirement coverage</h3><ul>{architecture.requirement_coverage.map((coverage, index) => <li key={`${index}-${coverage.requirement}`}><strong>{coverage.requirement}</strong> — {coverage.missing_information ? "Needs clarification" : coverage.covered_by.join(", ")}{coverage.notes ? ` (${coverage.notes})` : ""}</li>)}</ul></div>}
      {!architecture.application_architecture?.length && !architecture.design_decisions?.length && <p className="muted-copy">Architecture data is available. Sections are shown when provided by the design.</p>}
    </div>
  );
}

function TaskList({ tasks }) {
  return <div className="task-list">{tasks.map((task, index) => <div className="task-item" key={task.task_id}><span className={`task-number task-${task.status}`}>{task.status === "completed" ? "✓" : String(index + 1).padStart(2, "0")}</span><div className="task-copy"><strong>{task.title}</strong><p>{task.description}</p>{task.dependencies?.length > 0 && <small><Icon name="branch" size={12} /> Depends on {task.dependencies.join(", ")}</small>}</div><div className="task-meta"><Badge tone={priorityTone(task.priority)}>{task.priority}</Badge><StatusBadge status={task.status} /><small>{friendlyAgent(task.assigned_agent)}</small></div></div>)}</div>;
}

function AgentActivityTab({ logs, currentStage, projectStatus, details }) {
  const taskCount = details?.development_tasks?.length || 0;
  const fileCount = details?.generated_files?.length || 0;
  const latestTest = details?.test_results?.at(-1);
  const latestReview = details?.code_review_results?.at(-1);
  const latestAttempt = details?.debugging_attempts?.at(-1);
  const stagePosition = ["planning", "architecture", "development", "testing", "review", "debugging", "complete"].indexOf(currentStage);
  const agentStatus = (id) => {
    if (id === "project_manager") return details && currentStage !== "intake" ? "completed" : projectStatus === "in_progress" && currentStage === "intake" ? "running" : "pending";
    if (id === "architect") return details?.architecture_design ? "completed" : currentStage === "architecture" && projectStatus === "in_progress" ? "running" : "pending";
    if (id === "developer") return stagePosition > 2 || (fileCount > 0 && details?.development_tasks?.every((task) => task.status === "completed")) ? "completed" : currentStage === "development" && projectStatus === "in_progress" ? "running" : "pending";
    if (id === "tester") return latestTest ? "completed" : currentStage === "testing" && projectStatus === "in_progress" ? "running" : "pending";
    if (id === "reviewer") return latestReview ? "completed" : currentStage === "review" && projectStatus === "in_progress" ? "running" : "pending";
    if (id === "debugger") return latestAttempt ? "completed" : currentStage === "debugging" && projectStatus === "in_progress" ? "running" : "pending";
    return "pending";
  };
  const summaries = {
    project_manager: details ? `${taskCount} tasks planned` : "No planning output recorded",
    architect: details?.architecture_design?.technology_stack?.join(" · ") || "No design output recorded",
    developer: `${fileCount} generated files`,
    tester: latestTest ? `${latestTest.passed_tests} passed, ${latestTest.failed_tests} failed · ${latestTest.execution_status}` : "No test result recorded",
    reviewer: latestReview ? `Score ${latestReview.quality_score}/100 · ${latestReview.approved ? "approved" : "changes requested"}` : "No review output recorded",
    debugger: latestAttempt ? `${latestAttempt.hypothesis} · retest required` : "Only invoked for failed quality gates",
  };
  return (
    <div className="agent-page-grid">
      <section className="panel all-agents-panel"><PanelTitle kicker="SPECIALIST TEAM" title="Agent execution status" count={AGENTS.length} /><p className="panel-description">Completion is based on persisted agent outputs; checkpoint timestamps are shown only where the API recorded them.</p><div className="all-agents-grid">{AGENTS.map((agent) => { const state = agentStatus(agent.id); const lastCheckpoint = [...logs].reverse().find((log) => log.message.includes(`Workflow stage: ${currentStage}.`)); return <article className={`agent-card ${state === "running" ? "agent-card-active" : ""}`} key={agent.id}><div className="agent-card-top"><span className={`agent-avatar agent-${agent.id}`}><Icon name={agent.icon} size={18} /></span><span className={`agent-state-dot ${state === "running" ? "state-active" : state === "completed" ? "state-done" : ""}`} /></div><h3>{agent.name}</h3><p>{agent.role}</p><div className="agent-card-bottom"><StatusBadge status={state} />{state === "running" && <span className="agent-working"><i /> Working</span>}</div><p className="agent-output">{summaries[agent.id]}</p>{lastCheckpoint && state === "running" && <small className="agent-time">Checkpoint {formatDate(lastCheckpoint.timestamp)}</small>}</article>; })}</div></section>
      <section className="panel execution-history"><PanelTitle kicker="WORKFLOW EVENTS" title="Execution log" count={logs.length} />{logs.length ? <div className="execution-table"><div className="execution-table-head"><span>AGENT</span><span>EVENT</span><span>STATUS</span><span>TIME</span></div>{[...logs].reverse().map((log, index) => <div className="execution-row" key={`${log.timestamp}-${index}`}><strong>{friendlyAgent(log.agent_name)}</strong><span>{log.message}</span><StatusBadge status={log.status} /><time>{formatDate(log.timestamp)}</time></div>)}</div> : <EmptyState icon="clock" title="No activity recorded" detail="Start a project to see workflow events and agent progress." />}</section>
    </div>
  );
}

function CodeTab({ files, selectedFile, setSelectedFile, project, execution, onExecute }) {
  const [fileContent, setFileContent] = useState(null);
  const [fileError, setFileError] = useState("");
  const [fileLoading, setFileLoading] = useState(false);
  const [comparePrevious, setComparePrevious] = useState(false);
  const [entrypoint, setEntrypoint] = useState("main.py");
  const selectedFilePath = selectedFile?.file_path;
  const selectedFileVersion = selectedFile?.version;
  const versions = files.filter((file) => file.file_path === selectedFile?.file_path).sort((a, b) => a.version - b.version);
  const previous = versions.find((file) => file.version === selectedFile?.version - 1);
  useEffect(() => {
    let active = true;
    setFileContent(null);
    setFileError("");
    if (!selectedFilePath) return () => { active = false; };
    setFileLoading(true);
    api.getProjectFile(project.id, selectedFilePath, selectedFileVersion)
      .then(({ data }) => { if (active) setFileContent(data); })
      .catch((error) => { if (active) setFileError(getErrorMessage(error)); })
      .finally(() => { if (active) setFileLoading(false); });
    return () => { active = false; };
  }, [project.id, selectedFilePath, selectedFileVersion]);
  const latestFiles = useMemo(() => [...new Map(files.map((file) => [file.file_path, file])).values()], [files]);
  const tree = buildFileTree(latestFiles);
  useEffect(() => {
    const preferred = ["main.py", "app/main.py"].find((path) => latestFiles.some((file) => file.file_path === path));
    if (preferred) setEntrypoint(preferred);
    else if (!latestFiles.some((file) => file.file_path === entrypoint)) setEntrypoint("");
  }, [latestFiles, entrypoint]);
  const language = prismLanguage(selectedFile?.file_path);
  const grammar = Prism.languages[language] || Prism.languages.plain;
  const highlighted = fileContent ? Prism.highlight(fileContent.content, grammar, language) : "";
  const [copied, setCopied] = useState(false);
  const copyFile = async () => {
    if (!fileContent) return;
    try {
      await navigator.clipboard.writeText(fileContent.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setFileError("Clipboard access was denied by this browser.");
    }
  };
  return <div className="code-workbench"><section className="panel code-panel"><div className="code-panel-heading"><PanelTitle kicker="PROJECT SOURCE" title="Code explorer" count={latestFiles.length} /><span className="code-language"><span className="language-dot" /> Actual files · API</span></div>{latestFiles.length ? <div className="code-explorer"><div className="file-tree"><div className="tree-root"><Icon name="folder" size={14} /> {project.name}</div><FileTree nodes={tree} selected={selectedFile?.file_path} onSelect={(file) => { setSelectedFile(file); setComparePrevious(false); }} /></div><div className="code-preview"><div className="code-preview-bar"><div><span className="window-dot window-red" /><span className="window-dot window-yellow" /><span className="window-dot window-green" /></div><span>{selectedFile?.file_path || "Select a file"}</span><span className="code-version">v{selectedFile?.version || ""}</span></div>{selectedFile && <div className="code-toolbar"><span>{fileContent?.programming_language || language} · {formatDate(selectedFile.timestamp)}</span><div>{versions.length > 1 && <select aria-label="File version" value={selectedFile.version} onChange={(event) => { setSelectedFile(versions.find((file) => file.version === Number(event.target.value))); setComparePrevious(false); }}>{versions.map((file) => <option key={file.version} value={file.version}>Version {file.version}{file.version === versions.at(-1).version ? " · latest" : ""}</option>)}</select>}{previous && <button className="button button-quiet button-small" onClick={() => setComparePrevious((value) => !value)}>{comparePrevious ? "Hide previous" : `Compare v${previous.version}`}</button>}<button className="button button-quiet button-small" disabled={!fileContent} onClick={copyFile}>{copied ? "Copied" : "Copy code"}</button></div></div>}{fileError && <Alert message={fileError} />}{fileLoading ? <Loading label="Loading source file" /> : fileContent ? comparePrevious && previous ? <FileVersionComparison projectId={project.id} previous={previous} current={fileContent} /> : <pre className="source-code"><code className={`language-${language}`} dangerouslySetInnerHTML={{ __html: highlighted }} /></pre> : !fileError && <EmptyState icon="file" title="Select a file" detail="Choose a generated file to load its persisted source." />}</div></div> : <EmptyState icon="code" title="Your code will appear here" detail="No generated file records are available yet." />}</section><section className="panel execute-panel"><div><PanelTitle kicker="ISOLATED RUNTIME" title="Run generated application" icon="code" /><p>Execution happens in the existing resource-limited Docker sandbox with networking disabled. Only this project’s saved files are mounted.</p></div><div className="execute-controls"><label>Python entry point<input value={entrypoint} onChange={(event) => setEntrypoint(event.target.value)} placeholder="main.py" /></label><button className="button button-primary" disabled={!entrypoint.trim() || !latestFiles.some((file) => file.file_path === entrypoint.trim()) || execution?.status === "running"} onClick={() => onExecute(entrypoint.trim())}>{execution?.status === "running" ? "Running…" : "Run in Docker"}</button></div>{execution && <div className="execution-result"><div className="execution-result-header"><strong>{execution.entrypoint}</strong><StatusBadge status={execution.status} />{execution.result?.execution_duration != null && <small>{execution.result.execution_duration.toFixed(2)}s</small>}</div>{execution.result?.execution_error && <div className="execution-error"><strong>Execution error</strong><p>{execution.result.execution_error}</p></div>}{execution.result?.exit_code != null && <p>Exit code: <strong>{execution.result.exit_code}</strong></p>}{execution.result?.stdout && <OutputBlock title="Standard output" value={execution.result.stdout} />}{execution.result?.stderr && <OutputBlock title="Standard error" value={execution.result.stderr} />}{!execution.result && execution.status === "running" && <Loading label="Waiting for container result" />}</div>}</section></div>;
}

function FileVersionComparison({ projectId, previous, current }) {
  const [previousContent, setPreviousContent] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.getProjectFile(projectId, previous.file_path, previous.version)
      .then(({ data }) => { if (active) setPreviousContent(data.content); })
      .catch((requestError) => { if (active) setError(getErrorMessage(requestError)); });
    return () => { active = false; };
  }, [projectId, previous.file_path, previous.version]);
  if (error) return <Alert message={error} />;
  if (previousContent === null) return <Loading label="Loading previous version" />;
  return <div className="version-comparison"><section><h4>Previous · v{previous.version}</h4><pre className="source-code"><code>{previousContent}</code></pre></section><section><h4>Current · v{current.version}</h4><pre className="source-code"><code>{current.content}</code></pre></section></div>;
}

function FileTree({ nodes, selected, onSelect, prefix = "" }) {
  return <div className="file-tree-nodes">{nodes.map((node) => {
    const path = prefix ? `${prefix}/${node.name}` : node.name;
    return node.children ? <div className="tree-folder" key={path}><div className="tree-folder-name"><Icon name="folder" size={14} />{node.name}</div><FileTree nodes={node.children} prefix={path} selected={selected} onSelect={onSelect} /></div> : <button className={`tree-file ${selected === node.path ? "tree-selected" : ""}`} key={node.path} onClick={() => onSelect(node.file)}><Icon name="file" size={14} />{node.name}</button>;
  })}</div>;
}

function TestingTab({ runs, workflow, details, execution, project }) {
  const testHistory = details?.test_results || [];
  const latest = testHistory.at(-1);
  const review = details?.code_review_results?.at(-1);
  const attempts = details?.debugging_attempts || [];
  const rubricLabels = {
    functional_correctness: "Functional correctness",
    readability: "Readability",
    maintainability: "Maintainability",
    oop_principles: "OOP principles",
    error_handling: "Error handling",
    security: "Security",
    architecture_consistency: "Architecture consistency",
  };
  const verified = Boolean(project.status === "completed" && review?.approved && latest && latest.failed_tests === 0 && !latest.execution_error);
  return (
    <div className="testing-layout">
      <div className="testing-main">
        <section className="panel testing-summary"><PanelTitle kicker="APPLICATION SANDBOX" title="Docker execution" icon="code" />{execution?.result ? <><div className="execution-result-header"><strong>{execution.entrypoint}</strong><StatusBadge status={execution.status} /></div><div className="test-metadata-grid"><span>Exit code</span><strong>{execution.result.exit_code ?? "Not reported"}</strong><span>Duration</span><strong>{execution.result.execution_duration != null ? `${execution.result.execution_duration.toFixed(2)}s` : "Not reported"}</strong><span>Started</span><strong>{formatDate(execution.timestamp)}</strong></div>{execution.result.execution_error && <div className="execution-error"><strong>Execution error</strong><p>{execution.result.execution_error}</p></div>}{execution.result.stdout && <OutputBlock title="Standard output" value={execution.result.stdout} />}{execution.result.stderr && <OutputBlock title="Standard error" value={execution.result.stderr} />}</> : execution?.status === "running" ? <Loading label="Application is running inside Docker" /> : <InlineEmpty text="No application execution has been requested." />}</section>
        <section className="panel testing-summary"><PanelTitle kicker="QUALITY GATE" title="Testing overview" icon="check" />{latest ? <><div className="test-score-row"><div className="test-total"><strong>{latest.total_tests}</strong><span>Total tests</span></div><div className="test-pass"><strong>{latest.passed_tests}</strong><span>Passed</span></div><div className="test-fail"><strong>{latest.failed_tests}</strong><span>Failed</span></div><StatusBadge status={latest.execution_status || "unknown"} /></div><div className="test-progress"><span style={{ width: `${latest.total_tests ? (latest.passed_tests / latest.total_tests) * 100 : 0}%` }} /></div><div className="test-run-time">Latest persisted run{latest.execution_environment ? ` · ${latest.execution_environment}` : ""}{latest.execution_duration != null ? ` · ${latest.execution_duration.toFixed(2)}s` : ""}</div><div className="test-metadata-grid"><span>Exit code</span><strong>{latest.exit_code ?? "Not reported"}</strong><span>Execution status</span><strong>{latest.execution_status || "Not reported"}</strong><span>Environment</span><strong>{latest.execution_environment || "Not reported"}</strong></div>{latest.execution_error && <div className="execution-error"><strong>Execution error</strong><p>{latest.execution_error}</p></div>}{latest.stdout && <OutputBlock title="Standard output" value={latest.stdout} />}{latest.stderr && <OutputBlock title="Standard error" value={latest.stderr} />}{latest.error_details?.map((error, index) => <div className="test-error" key={`${index}-${error}`}>{error}</div>)}</> : <EmptyState icon="check" title="No test results yet" detail="Test results appear after the Testing Agent runs the generated project in Docker." />}</section>
        <section className="panel test-history"><PanelTitle kicker="RUN HISTORY" title="Test executions" count={runs.length} />{runs.length ? <div className="test-run-list">{[...runs].reverse().map((run, index) => <div className="test-run-item" key={`${run.timestamp}-${index}`}><span className={`run-icon ${run.failed_tests ? "run-failed" : "run-passed"}`}><Icon name={run.failed_tests ? "close" : "check"} size={15} /></span><div><strong>Test run #{runs.length - index}</strong><small>{formatDate(run.timestamp)}</small></div><span className="run-counts"><b className="text-green">{run.passed_tests} passed</b><b className="text-red">{run.failed_tests} failed</b></span><StatusBadge status={run.execution_status} /></div>)}</div> : <InlineEmpty text="No test executions recorded." />}</section>
        <section className="panel feedback-panel"><PanelTitle kicker="REVIEW FEEDBACK" title="Code quality" icon="scan" />{review ? <><div className="review-score-header"><strong>{review.quality_score}<small>/100</small></strong><div><StatusBadge status={review.approved ? "approved" : "rejected"} /><p>{review.approval_rationale}</p></div></div>{Object.keys(review.rubric_scores || {}).length > 0 && <div className="rubric-grid">{Object.entries(review.rubric_scores).map(([key, score]) => <div className="rubric-row" key={key}><span>{rubricLabels[key] || key}</span><strong>{score}/100</strong><span className="rubric-track"><i style={{ width: `${score}%` }} /></span></div>)}</div>}<ReviewFindings review={review} /></> : <InlineEmpty text="The reviewer has not returned a result yet." />}</section>
      </div>
      <aside className="testing-rail">
        <section className="panel approval-status-panel"><PanelTitle kicker="FINAL DECISION" title="Workflow outcome" icon="shield" /><div className={`final-status-illustration ${verified ? "final-success" : ["failed", "blocked"].includes(project.status) ? "final-failed" : ""}`}><span>{verified ? "✓" : ["failed", "blocked"].includes(project.status) ? "!" : "…"}</span></div><h3>{verified ? "Review and tests passed" : project.status === "completed" ? "Workflow completed; gate evidence incomplete" : project.status === "failed" ? "Workflow failed" : project.status === "blocked" ? "Workflow blocked" : project.status === "awaiting_approval" ? "Architecture review pending" : "Not finalized"}</h3><p>{workflow?.workflow_summary || "The workflow has not returned a final summary."}</p><StatusBadge status={project.status} /></section>
        <section className="panel debug-panel"><PanelTitle kicker="DEBUG CYCLE" title="Debugging attempts" icon="bug" />{attempts.length ? <div className="debug-attempt-list">{attempts.map((attempt, index) => <article className="debug-attempt" key={`${index}-${attempt.hypothesis}`}><div className="debug-attempt-heading"><strong>Attempt {index + 1} of {details.max_debugging_attempts}</strong><StatusBadge status={attempt.successful ? "correction proposed" : "failed"} /></div><p><b>Detected problem:</b> {attempt.issue}</p><p><b>Root cause:</b> {attempt.hypothesis}</p><p><b>Correction:</b> {attempt.action}</p><p><b>Files:</b> {(attempt.corrected_files || []).join(", ") || "No correction file recorded"}</p><p>{verified && index === attempts.length - 1 ? "Subsequent tests and review passed; workflow gates accepted the corrected project." : "Correction was proposed and saved; it is not verified until subsequent tests and review pass."}</p></article>)}</div> : <InlineEmpty text="No debugger invocation was recorded." />}<small>Retry limit: {details?.max_debugging_attempts ?? "Not reported"}</small></section>
        <div className="testing-safety"><Icon name="shield" size={16} /><span>Generated code is evaluated through the project's isolated Docker testing tools.</span></div>
      </aside>
    </div>
  );
}

function OutputBlock({ title, value }) {
  return <details className="output-block"><summary>{title}</summary><pre>{value}</pre></details>;
}

function ReviewFindings({ review }) {
  const groups = [
    ["Critical", review.critical_issues || [], "critical"],
    ["Major", review.major_issues || [], "major"],
    ["Minor", review.minor_suggestions || [], "minor"],
  ];
  return <div className="review-findings">{groups.map(([label, findings, tone]) => <section key={label}><h3><Badge tone={tone}>{label}</Badge> {findings.length}</h3>{findings.map((finding, index) => <article className="review-finding" key={`${index}-${finding.title}`}><div><strong>{finding.title}</strong><Badge tone={finding.confirmed ? "danger" : "neutral"}>{finding.confirmed ? "Confirmed" : "Potential"}</Badge></div><p>{finding.description}</p><small>{finding.file_path || "Project-wide"}{finding.line_number ? `:${finding.line_number}` : ""} · {Math.round(finding.confidence * 100)}% confidence</small><p className="finding-recommendation">{finding.recommendation}</p></article>)}</section>)}</div>;
}

function FinalReport({ project, workflow, details, files, onExplore }) {
  const review = details?.code_review_results?.at(-1);
  const test = details?.test_results?.at(-1);
  const gatesPassed = Boolean(review?.approved && test && test.total_tests > 0 && test.failed_tests === 0 && !test.execution_error);
  return <section className="panel final-report"><PanelTitle kicker="DELIVERY SUMMARY" title="Final project report" icon="check" /><div className="final-report-status"><StatusBadge status={project.status} /><strong>{gatesPassed ? "All recorded quality gates passed." : "Workflow status is complete, but quality-gate evidence is incomplete."}</strong></div><p>{workflow?.workflow_summary || project.description}</p><div className="final-report-grid"><div><span>Technologies</span><strong>{workflow?.architecture_design?.technology_stack?.join(", ") || "Not reported"}</strong></div><div><span>Generated files</span><strong>{new Set(files.map((file) => file.file_path)).size}</strong></div><div><span>Latest tests</span><strong>{test ? `${test.passed_tests} passed · ${test.failed_tests} failed` : "Not reported"}</strong></div><div><span>Review score</span><strong>{review ? `${review.quality_score}/100 · ${review.approved ? "approved" : "not approved"}` : "Not reported"}</strong></div><div><span>Issues addressed</span><strong>{details?.debugging_attempts?.length || 0} debugging attempt(s)</strong></div></div>{review?.identified_issues?.length > 0 && <div><h3>Remaining identified issues</h3><ul>{review.identified_issues.map((issue, index) => <li key={`${index}-${issue}`}>{issue}</li>)}</ul></div>}{details?.error_messages?.length > 0 && <div><h3>Workflow errors</h3><ul>{details.error_messages.map((message, index) => <li key={`${index}-${message}`}>{message}</li>)}</ul></div>}<button className="button button-secondary" onClick={onExplore}>Explore generated files</button></section>;
}

function InlineEmpty({ text }) {
  return <div className="inline-empty"><span className="inline-empty-mark">—</span>{text}</div>;
}

function buildFileTree(files) {
  const root = [];
  for (const file of files) {
    const parts = file.file_path.replaceAll("\\", "/").split("/").filter(Boolean);
    let current = root;
    parts.forEach((part, index) => {
      const isFile = index === parts.length - 1;
      let node = current.find((item) => item.name === part);
      if (!node) {
        node = isFile ? { name: part, path: file.file_path, file } : { name: part, children: [] };
        current.push(node);
      }
      if (!isFile) current = node.children;
    });
  }
  return root;
}

function friendlyAgent(name) {
  return AGENTS.find((agent) => agent.id === name)?.name || name?.replaceAll("_", " ") || "Workflow";
}

function formatStage(stage) {
  if (!stage) return "Not started";
  return stage.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function stageDescription(stage, status) {
  if (status === "pending") return "This project has not started yet.";
  if (status === "awaiting_approval") return "Architecture is ready for human review.";
  if (status === "completed") return "The workflow completed successfully.";
  if (status === "failed") return "The workflow stopped before completion.";
  const descriptions = {
    planning: "Breaking the project brief into dependency-aware tasks.",
    architecture: "Designing the application structure and technology choices.",
    development: "Generating project files from the approved design.",
    testing: "Running the generated project's isolated test suite.",
    review: "Evaluating implementation quality and requirements.",
    debugging: "Applying targeted corrections before another test run.",
  };
  return descriptions[stage] || "The workflow is progressing.";
}

function priorityTone(priority) {
  return priority === "critical" || priority === "high" ? "warning" : "neutral";
}

function prismLanguage(path = "") {
  const extension = path.split(".").pop().toLowerCase();
  return ({
    py: "python",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "typescript",
    json: "json",
    html: "markup",
    xml: "markup",
    css: "css",
    sql: "sql",
  })[extension] || "plain";
}

function truncate(value, limit) {
  if (!value) return "No description";
  return value.length > limit ? `${value.slice(0, limit).trimEnd()}…` : value;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

function formatRelative(value) {
  if (!value) return "—";
  const delta = Math.max(0, Date.now() - new Date(value).getTime());
  if (!Number.isFinite(delta)) return "—";
  const minutes = Math.floor(delta / 60000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export default App;
