"use client";

import type { components } from "@resume/shared/schema";
import { type ChangeEvent, useRef, useState } from "react";

import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { StatusTag } from "../../components/ui/StatusTag";
import { createWebApiClient } from "../../features/api/client";
import { useApiResource } from "../../features/api/useApiResource";
import type { SessionUser } from "../../features/session/ProtectedBoundary";

export default function SettingsPage() {
  const me = useApiResource<SessionUser>("/v1/me");
  const usage = useApiResource<components["schemas"]["UsageResponse"]>("/v1/me/usage");
  const [requestState, setRequestState] = useState("");
  const [deleteEmail, setDeleteEmail] = useState("");
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteConfirmed, setDeleteConfirmed] = useState(false);
  const privacyKeys = useRef<Record<string, string>>({});

  async function requestPrivacy(path: "/v1/me/data-exports" | "/v1/me/deletion-requests", label: string) {
    setRequestState("正在提交请求…");
    try {
      privacyKeys.current[path] ||= crypto.randomUUID();
      const task = await createWebApiClient().post<{}, components["schemas"]["PrivacyTaskResponse"]>(path, {}, privacyKeys.current[path]);
      delete privacyKeys.current[path];
      setRequestState(`${label}已进入任务中心：${task.id}`);
    } catch {
      setRequestState("请求失败，请稍后重试。");
    }
  }

  async function logout() {
    await createWebApiClient().post("/v1/auth/logout", {}, crypto.randomUUID());
    if (typeof localStorage !== "undefined") {
      for (let index = localStorage.length - 1; index >= 0; index -= 1) {
        const key = localStorage.key(index);
        if (key && ["resume-draft:", "intake-answer:", "job-draft:"].some((prefix) => key.startsWith(prefix))) {
          localStorage.removeItem(key);
        }
      }
    }
    window.location.assign("/login");
  }

  async function requestDeletion() {
    if (!deleteConfirmed || !deleteEmail.trim() || !deletePassword) return;
    setRequestState("正在重新验证身份…");
    try {
      const login: components["schemas"]["PasswordLoginRequest"] = {
        consents: null,
        email: deleteEmail.trim(),
        password: deletePassword,
      };
      await createWebApiClient().post("/v1/auth/password/login", login, crypto.randomUUID());
      await requestPrivacy("/v1/me/deletion-requests", "删除");
      setDeletePassword("");
    } catch {
      setRequestState("身份验证失败，未提交删除请求。");
    }
  }

  return (
    <Page eyebrow="账号、隐私与数据" title="个人设置">
      {(me.status === "loading" || usage.status === "loading") ? <section className="panel" role="status">正在读取账号信息…</section> : null}
      {(me.status === "error" || usage.status === "error") ? <section className="panel" role="alert"><p>{me.status === "error" ? me.error : usage.status === "error" ? usage.error : ""}</p><Button onClick={() => { me.reload(); usage.reload(); }} variant="secondary">重试</Button></section> : null}
      {me.status === "ready" && usage.status === "ready" ? (
        <div className="settings-list">
          <section className="panel"><h2>账号</h2><p>{me.data.masked_email ?? me.data.identity_type}</p><StatusTag tone="success">身份已验证</StatusTag><p>今日 AI 用量：{usage.data.ai_tasks_used} / {usage.data.ai_tasks_limit}</p><Button onClick={() => void logout()} variant="quiet">退出登录</Button></section>
          <section className="panel"><h2>数据副本</h2><p>请求一份账号、简历和事实记录的数据副本。</p><Button onClick={() => void requestPrivacy("/v1/me/data-exports", "数据导出")} variant="secondary">申请数据导出</Button></section>
          <section className="panel panel--danger">
            <h2>删除账号与数据</h2>
            <p>这会删除账号、简历和事实记录，并使当前会话失效。提交前必须重新输入邮箱与密码。</p>
            <Field label="完整邮箱" name="delete-email" onChange={(event: ChangeEvent<HTMLInputElement>) => setDeleteEmail(event.currentTarget.value)} value={deleteEmail} />
            <Field label="当前密码" name="delete-password" onChange={(event: ChangeEvent<HTMLInputElement>) => setDeletePassword(event.currentTarget.value)} type="password" value={deletePassword} />
            <label className="check-row">
              <input checked={deleteConfirmed} onChange={(event) => setDeleteConfirmed(event.currentTarget.checked)} type="checkbox" />
              <span>我了解删除完成后数据不可恢复</span>
            </label>
            <Button disabled={!deleteConfirmed || !deleteEmail.trim() || !deletePassword} onClick={() => void requestDeletion()} state="error" variant="secondary">验证并申请删除</Button>
          </section>
        </div>
      ) : null}
      {requestState ? <p className="panel" role="status">{requestState}</p> : null}
    </Page>
  );
}
