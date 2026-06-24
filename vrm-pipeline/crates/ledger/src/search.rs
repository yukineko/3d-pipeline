use crate::db::EmbeddingRow;

pub struct SimilarResult {
    pub id: String,
    pub score: f64,
}

fn cosine(a: &[f64], b: &[f64]) -> f64 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let dot: f64 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let na: f64 = a.iter().map(|x| x * x).sum::<f64>().sqrt();
    let nb: f64 = b.iter().map(|x| x * x).sum::<f64>().sqrt();
    if na == 0.0 || nb == 0.0 {
        0.0
    } else {
        dot / (na * nb)
    }
}

pub fn top_k_similar(query: &[f64], rows: &[EmbeddingRow], k: usize) -> Vec<SimilarResult> {
    let mut scored: Vec<SimilarResult> = rows
        .iter()
        .filter(|r| r.vec.len() == query.len())
        .map(|r| SimilarResult {
            id: r.id.clone(),
            score: cosine(query, &r.vec),
        })
        .collect();
    scored.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    scored.truncate(k);
    scored
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: &str, v: Vec<f64>) -> EmbeddingRow {
        EmbeddingRow { id: id.to_string(), vec: v }
    }

    #[test]
    fn identical_vector_scores_one() {
        let q = vec![1.0, 0.0, 0.0];
        let rows = vec![row("a", vec![1.0, 0.0, 0.0])];
        let r = top_k_similar(&q, &rows, 5);
        assert_eq!(r.len(), 1);
        assert!((r[0].score - 1.0).abs() < 1e-9);
    }

    #[test]
    fn orthogonal_vector_scores_zero() {
        let q = vec![1.0, 0.0];
        let rows = vec![row("a", vec![0.0, 1.0])];
        let r = top_k_similar(&q, &rows, 5);
        assert_eq!(r.len(), 1);
        assert!((r[0].score - 0.0).abs() < 1e-9);
    }

    #[test]
    fn ordering_is_descending_by_score() {
        let q = vec![1.0, 0.0, 0.0];
        let rows = vec![
            row("close", vec![0.9, 0.1, 0.0]),
            row("far",   vec![0.1, 0.9, 0.0]),
            row("exact", vec![1.0, 0.0, 0.0]),
        ];
        let r = top_k_similar(&q, &rows, 3);
        assert_eq!(r[0].id, "exact");
        assert!(r[0].score >= r[1].score);
        assert!(r[1].score >= r[2].score);
    }

    #[test]
    fn dimension_mismatch_skipped() {
        let q = vec![1.0, 0.0];
        let rows = vec![
            row("ok",  vec![1.0, 0.0]),
            row("bad", vec![1.0, 0.0, 0.0]),
        ];
        let r = top_k_similar(&q, &rows, 5);
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].id, "ok");
    }

    #[test]
    fn top_k_limits_results() {
        let q = vec![1.0, 0.0];
        let rows: Vec<EmbeddingRow> = (0..10)
            .map(|i| row(&format!("r{i}"), vec![i as f64, 0.0]))
            .collect();
        let r = top_k_similar(&q, &rows, 3);
        assert_eq!(r.len(), 3);
    }
}
