import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Button, Card, Form, Input, message, Typography } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { api } from "../api/client";

const { Title } = Typography;

export default function Register() {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const res = await api.register(values.username, values.password);
      localStorage.setItem("token", res.access_token);
      localStorage.setItem("username", res.username);
      message.success("registered successfully");
      navigate("/");
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : "registration failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", background: "#f0f2f5" }}>
      <Card style={{ width: 400 }}>
        <Title level={3} style={{ textAlign: "center", marginBottom: 32 }}>Cairn</Title>
        <Form onFinish={onFinish} size="large">
          <Form.Item name="username" rules={[
            { required: true, message: "please enter username" },
            { min: 3, message: "at least 3 characters" },
          ]}>
            <Input prefix={<UserOutlined />} placeholder="Username" />
          </Form.Item>
          <Form.Item name="password" rules={[
            { required: true, message: "please enter password" },
            { min: 6, message: "at least 6 characters" },
          ]}>
            <Input.Password prefix={<LockOutlined />} placeholder="Password" />
          </Form.Item>
          <Form.Item name="confirm" dependencies={["password"]} rules={[
            { required: true, message: "please confirm password" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("password") === value) return Promise.resolve();
                return Promise.reject(new Error("passwords do not match"));
              },
            }),
          ]}>
            <Input.Password prefix={<LockOutlined />} placeholder="Confirm Password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block>
              Register
            </Button>
          </Form.Item>
        </Form>
        <div style={{ textAlign: "center" }}>
          <Link to="/login">Already have an account? Login</Link>
        </div>
      </Card>
    </div>
  );
}
