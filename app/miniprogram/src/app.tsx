import { PropsWithChildren } from "react";
import Taro, { useDidHide, useDidShow } from "@tarojs/taro";
import { flushRegisteredDrafts, refreshRegisteredResources } from "./features/draft-store";
import "./app.scss";

export default function App({ children }: PropsWithChildren) {
  useDidHide(() => {
    void flushRegisteredDrafts();
  });
  useDidShow(() => {
    void refreshRegisteredResources();
  });
  Taro.onError((message) => console.error("miniprogram_error", { message }));
  return children;
}
