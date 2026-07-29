import Taro from "@tarojs/taro";

export async function getStored<T>(key: string): Promise<T | undefined> {
  try {
    return (await Taro.getStorage<T>({ key })).data;
  } catch {
    return undefined;
  }
}

export async function setStored<T>(key: string, value: T): Promise<void> {
  await Taro.setStorage({ key, data: value });
}

export async function removeStored(key: string): Promise<void> {
  await Taro.removeStorage({ key });
}
