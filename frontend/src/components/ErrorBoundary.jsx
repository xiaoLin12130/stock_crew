import React from "react";

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, message: error && error.message ? error.message : String(error) };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="error-boundary">
          <div className="error-icon">⚠️</div>
          <div className="error-title">页面出现异常</div>
          <div className="error-detail">{this.state.message || "未知错误，请返回重试"}</div>
          <button
            className="btn btn-accent"
            onClick={() => {
              this.setState({ hasError: false, message: "" });
              if (this.props.onReset) this.props.onReset();
            }}
          >
            返回首页
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
