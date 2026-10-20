export interface Bookmark {
  id: number;
  url: string;
  title: string;
  tags: string[];
  created_at: string;
}

export interface BookmarkInput {
  url: string;
  title: string;
  tags: string[];
}
