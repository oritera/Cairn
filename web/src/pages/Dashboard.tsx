import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Layout, Space, Table, Tag, Typography } from "antd";
import { LogoutOutlined, PlusOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { api, type ProjectSummary } from "../api/client";

const { Header, Content } = Layout;
const { Title } = Typography;

const columns: ColumnsType<ProjectSummary> = [
  { title: "ID", dataIndex: "id", key: "id", width: 80 },
  { title: "Title", dataIndex: "title", key: "title" },
  {
    title: "Status",
    dataIndex: "status",
    key: "status",
    render: (status: string) => {
      const color = status === "active" ? "green" : status === "completed" ? "blue" : "default";
      return <Tag color={color}>{status}</Tag>;
    },
  },
  { title: "Facts", dataIndex: "fact_count", key: "facts", width: 80 },
  { title: "Intents", dataIndex: "intent_count", key: "intents", width: 80 },
  { title: "Working", dataIndex: "working_intent_count", key: "working", width: 80 },
  { title: "Created", dataIndex: "created_at", key: "created_at", width: 180 },
];

export default function Dashboard() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();
  const username = localStorage.getItem("username") || "";

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {}).finally(() => setLoading(false));
  }, []);

  const logout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("username");
    navigate("/login");
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Title level={4} style={{ color: "#fff", margin: 0 }}>Cairn</Title>
        <Space>
          <span style={{ color: "#fff" }}>{username}</span>
          <Button icon={<LogoutOutlined />} onClick={logout} type="text" style={{ color: "#fff" }}>
            Logout
          </Button>
        </Space>
      </Header>
      <Content style={{ padding: 24 }}>
        <Card
          title="Projects"
          extra={<Button type="primary" icon={<PlusOutlined />}>New Project</Button>}
        >
          <Table
            columns={columns}
            dataSource={projects}
            rowKey="id"
            loading={loading}
            size="middle"
          />
        </Card>
      </Content>
    </Layout>
  );
}
