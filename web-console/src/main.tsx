import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "./index.css";
import "./supervision.css";
import "./platform.css";
import "./duty-console.css";
import "./workspace.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#177c73",
          colorText: "#20383f",
          colorTextSecondary: "#71838a",
          colorBorderSecondary: "#e4eaf1",
          colorBgLayout: "#f3f6f6",
          borderRadius: 7,
          fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
          fontSize: 14,
        },
        components: {
          Table: { headerBg: "#f6f8fb", cellPaddingBlock: 14 },
          Card: { headerFontSize: 15 },
          Menu: {
            itemHeight: 42,
            itemSelectedBg: "#eaf1fa",
            itemSelectedColor: "#177c73",
          },
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>,
);
