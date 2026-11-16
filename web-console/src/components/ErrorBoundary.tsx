// 全局错误边界：组件异常时显示错误卡片而非整页白屏。
import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { Button, Result } from 'antd'

interface Props { children: ReactNode; inline?: boolean }
interface State { error: Error | null }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('页面渲染异常:', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      // 局部模式：卡内提示，保留页面高度与滚动位置（避免整页塌陷导致滚动归零）
      return <div style={{ minHeight: this.props.inline ? 200 : undefined, padding: this.props.inline ? 16 : 0 }}>
        <Result status="warning" title={this.props.inline ? '该模块渲染出错' : '页面渲染出错'}
          subTitle={<span style={{ color: '#7891ac' }}>{this.state.error.message}</span>}
          extra={<Button type="primary" onClick={() => { this.setState({ error: null }); if (!this.props.inline) window.location.reload() }}>重试</Button>} />
      </div>
    }
    return this.props.children
  }
}
