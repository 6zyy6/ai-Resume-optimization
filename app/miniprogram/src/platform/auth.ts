import Taro from "@tarojs/taro";
import { newIdempotencyKey, write } from "./request";

export interface WechatSession {
  user_id: string;
  expires_at: string;
}

export async function loginFromUserAction(): Promise<WechatSession> {
  const { code } = await Taro.login();
  if (!code) throw new Error("微信登录未返回授权码，请重试。");
  return write("post", "/v1/auth/wechat/login", { code }, newIdempotencyKey("wechat-login"));
}

export async function logout(): Promise<void> {
  await write("post", "/v1/auth/logout", {}, newIdempotencyKey("logout"));
}
