import { Link, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { EmptyState } from './components/States';
import { Admin } from './pages/Admin';
import { Dashboard } from './pages/Dashboard';
import { Documents } from './pages/Documents';
import { Facts } from './pages/Facts';
import { Ingest } from './pages/Ingest';
import { JobDetail } from './pages/JobDetail';
import { Jobs } from './pages/Jobs';
import { SearchPage } from './pages/SearchPage';
import { TreePage } from './pages/TreePage';

function NotFound(): JSX.Element {
  return (
    <main className="page">
      <div className="card">
        <EmptyState
          title="Page not found"
          text="That route does not exist in the console."
          action={
            <Link className="btn btn-sm btn-primary" to="/">
              Back to dashboard
            </Link>
          }
        />
      </div>
    </main>
  );
}

export function App(): JSX.Element {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/ingest" element={<Ingest />} />
        <Route path="/jobs" element={<Jobs />} />
        {/* Job detail is `/job?id=…`, not `/jobs/:id`, on purpose — see the
            route-depth note in vite.config.ts. */}
        <Route path="/job" element={<JobDetail />} />
        <Route path="/documents" element={<Documents />} />
        <Route path="/tree" element={<TreePage />} />
        <Route path="/facts" element={<Facts />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/admin" element={<Admin />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
