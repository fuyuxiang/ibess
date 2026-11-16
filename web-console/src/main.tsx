import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "./index.css";
import "./supervision.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#2165b4",
          colorText: "#24364b",
          colorTextSecondary: "#64758b",
          colorBorderSecondary: "#e4eaf1",
          colorBgLayout: "#f3f5f8",
          borderRadius: 6,
          fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
          fontSize: 14,
        },
        components: {
          Table: { headerBg: "#f6f8fb", cellPaddingBlock: 14 },
          Card: { headerFontSize: 15 },
          Menu: {
            itemHeight: 42,
            itemSelectedBg: "#eaf1fa",
            itemSelectedColor: "#18599f",
          },
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>,
);
