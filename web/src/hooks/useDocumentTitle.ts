import { useEffect } from "react";

/** Sets document.title while mounted and restores the previous title on unmount. */
export function useDocumentTitle(title: string) {
  useEffect(() => {
    const prev = document.title;
    return () => {
      document.title = prev;
    };
  }, []);
  useEffect(() => {
    document.title = title;
  }, [title]);
}
