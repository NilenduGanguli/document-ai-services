import { Component, type ErrorInfo, type ReactNode } from 'react';
import { IconAlert } from './Icons';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Last-resort boundary: a render crash shows a recoverable panel instead of a
 * blank page. Route-level failures are handled by each page's error state.
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Kept for the browser console — the operator's first debugging surface.
    console.error('Console crashed:', error, info.componentStack);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <main className="page">
        <div className="card">
          <div className="state error">
            <span className="state-icon">
              <IconAlert size={20} />
            </span>
            <span className="state-title">The console hit an unexpected error</span>
            <p className="state-text">{error.message}</p>
            <div className="row">
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => this.setState({ error: null })}
              >
                Try again
              </button>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => window.location.reload()}
              >
                Reload
              </button>
            </div>
          </div>
        </div>
      </main>
    );
  }
}
