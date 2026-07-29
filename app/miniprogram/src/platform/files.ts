import Taro from "@tarojs/taro";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

export interface UploadTarget {
  uploadUrl: string;
  mimeType: string;
}

export async function uploadBinary(filePath: string, target: UploadTarget): Promise<void> {
  const file = Taro.getFileSystemManager().readFileSync(filePath);
  const response = await Taro.request({
    url: target.uploadUrl,
    method: "PUT",
    data: file,
    header: { "Content-Type": target.mimeType },
  });
  if (response.statusCode < 200 || response.statusCode >= 300) {
    throw new Error("文件上传失败，请检查网络后重试。");
  }
}

export async function saveRemoteFile(url: string): Promise<{ saved: boolean; alternative?: string }> {
  try {
    const downloaded = await Taro.downloadFile({ url });
    if (downloaded.statusCode !== 200) throw new Error("download failed");
    if (typeof Taro.saveFile !== "function") {
      return { saved: false, alternative: "当前微信版本不支持保存文件，请在浏览器中打开下载链接。" };
    }
    await Taro.saveFile({ tempFilePath: downloaded.tempFilePath });
    return { saved: true };
  } catch {
    return { saved: false, alternative: "暂时无法保存，请复制下载链接后在浏览器打开。" };
  }
}

export function chooseResumeFile() {
  return Taro.chooseMessageFile({
    count: 1,
    type: "file",
    extension: ["pdf", "docx", "txt"],
  });
}

export async function inspectFile(filePath: string): Promise<{ sha256: string; size: number }> {
  const file = Taro.getFileSystemManager().readFileSync(filePath) as ArrayBuffer;
  return { sha256: bytesToHex(sha256(new Uint8Array(file))), size: file.byteLength };
}
