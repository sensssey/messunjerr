export interface Post {
  created_at: string | number | Date;
  id: string; // Идентификатор поста
  title: string; // Заголовок поста
  content: string; // Содержимое поста
}